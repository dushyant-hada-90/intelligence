"""TikTok keyword SERP via httpx + logged-in cookies from .env (no Playwright).

Requires session cookies in backend/.env (same idea as INSTAGRAM_SESSIONID).
Endpoint: GET https://www.tiktok.com/api/search/item/full/
"""

from __future__ import annotations

import json
import logging
import random
import re
import string
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from config.tunables import (
    DISCOVER_RESULTS_PER_QUERY,
    DISCOVER_TT_MAX_DELAY_SEC,
    DISCOVER_TT_MIN_DELAY_SEC,
)
from platforms.rate_limit import JitterLimiter
from platforms.registry import PlatformSpec, engagement_from_counts, register
from platforms.safe_search import safe_search
from platforms.tiktok.cookies import cookie_header, require_session_cookies
from shared.http import httpx_client

log = logging.getLogger("tt-search")

_TT_LIMITER = JitterLimiter(DISCOVER_TT_MIN_DELAY_SEC, DISCOVER_TT_MAX_DELAY_SEC)


def _rand_device_id() -> str:
    return str(random.randint(7_250_000_000_000_000_000, 7_325_099_899_999_994_577))


def _rand_verify_fp() -> str:
    alphabet = string.ascii_letters + string.digits
    body = "".join(random.choice(alphabet) for _ in range(32))
    return f"verify_{body}"


def _merge_set_cookie(cookies: dict[str, str], resp: httpx.Response) -> dict[str, str]:
    """Apply Set-Cookie into our dict; last value wins per name."""
    updated = dict(cookies)
    # httpx may expose jar; prefer header parse to dodge duplicate-name errors
    raw_list: list[str] = []
    try:
        get_list = getattr(resp.headers, "get_list", None)
        if callable(get_list):
            raw_list = list(get_list("set-cookie") or [])
    except Exception:
        raw_list = []
    if not raw_list:
        single = resp.headers.get("set-cookie")
        if single:
            raw_list = [single]
    for raw in raw_list:
        # First segment is name=value; rest are attributes
        first = (raw or "").split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            updated[name] = value
    # Safe jar merge fallback (ignore duplicate-name errors)
    try:
        for name, value in resp.cookies.items():
            updated[name] = value
    except Exception:
        pass
    return updated


def _client() -> httpx.Client:
    headers = {
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }
    # No cookies= jar — we send Cookie header ourselves each request.
    return httpx_client(timeout=45.0, headers=headers)


def _normalize_item(item: dict[str, Any], query: str) -> Optional[dict[str, Any]]:
    vid = str(item.get("id") or "").strip()
    if not vid:
        return None
    author = item.get("author") or {}
    username = str(author.get("uniqueId") or author.get("unique_id") or "").strip()
    stats = item.get("stats") or item.get("statsV2") or {}
    video = item.get("video") or {}

    def _stat(key: str) -> Optional[int]:
        val = stats.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    play = _stat("playCount")
    digg = _stat("diggCount")
    comments = _stat("commentCount")
    engagement, eng_src = engagement_from_counts(play, digg)
    taken = item.get("createTime") or item.get("create_time") or 0
    try:
        taken_at = int(taken)
    except (TypeError, ValueError):
        taken_at = 0

    cover = video.get("cover") or video.get("originCover") or video.get("dynamicCover")
    url = (
        f"https://www.tiktok.com/@{username}/video/{vid}"
        if username
        else f"https://www.tiktok.com/video/{vid}"
    )
    return {
        "id": vid,
        "pk": vid,
        "code": vid,
        "platform": "tiktok",
        "url": url,
        "caption": str(item.get("desc") or ""),
        "username": username,
        "taken_at": taken_at,
        "view_count": play,
        "like_count": digg,
        "comment_count": comments,
        "engagement": engagement,
        "engagement_source": eng_src,
        "thumbnail_url": cover,
        "query": query,
    }


def parse_item_list(payload: dict[str, Any], query: str) -> list[dict[str, Any]]:
    items = payload.get("item_list") or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        norm = _normalize_item(item, query)
        if not norm:
            continue
        key = norm["id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
        if len(out) >= DISCOVER_RESULTS_PER_QUERY:
            break
    return out


def _extract_embedded_items(html: str, query: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    patterns = [
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        r'<script id="SIGI_STATE"[^>]*>(.*?)</script>',
    ]
    blobs: list[Any] = []
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            blobs.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue

    def walk(obj: Any) -> None:
        if len(out) >= DISCOVER_RESULTS_PER_QUERY:
            return
        if isinstance(obj, dict):
            if "item_list" in obj and isinstance(obj["item_list"], list):
                for item in obj["item_list"]:
                    if isinstance(item, dict):
                        norm = _normalize_item(item, query)
                        if norm and norm["id"] not in {r["id"] for r in out}:
                            out.append(norm)
                            if len(out) >= DISCOVER_RESULTS_PER_QUERY:
                                return
            if obj.get("id") and (obj.get("desc") is not None) and (
                "stats" in obj or "author" in obj
            ):
                norm = _normalize_item(obj, query)
                if norm and norm["id"] not in {r["id"] for r in out}:
                    out.append(norm)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for blob in blobs:
        walk(blob)
    return out[:DISCOVER_RESULTS_PER_QUERY]


def _debug_body(resp: httpx.Response) -> str:
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    body = resp.text or ""
    return (
        f"http={resp.status_code} content-type={ctype or 'unknown'!r} "
        f"bytes={len(body)} body≈{(body[:160] or '(empty)').replace(chr(10), ' ')!r}"
    )


def search_keyword(query: str) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    cookies = require_session_cookies()
    has_ms = bool(cookies.get("msToken"))
    has_ttwid = bool(cookies.get("ttwid"))

    with _TT_LIMITER.lock:
        _TT_LIMITER.wait()
        try:
            jar = dict(cookies)
            with _client() as client:
                referer = f"https://www.tiktok.com/search/video?q={quote(q)}"
                # Refresh Set-Cookie if the session is still valid.
                html_resp = client.get(
                    referer,
                    headers={
                        "Accept": "text/html",
                        "Referer": "https://www.tiktok.com/",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "same-origin",
                        "Cookie": cookie_header(jar),
                    },
                )
                jar = _merge_set_cookie(jar, html_resp)
                embedded = _extract_embedded_items(html_resp.text or "", q)
                if embedded:
                    log.info("TT keyword search q=%r via embedded HTML n=%s", q, len(embedded))
                    return embedded

                ms = jar.get("msToken") or cookies.get("msToken")
                verify = jar.get("s_v_web_id") or cookies.get("s_v_web_id") or _rand_verify_fp()

                params: dict[str, str] = {
                    "WebIdLastTime": str(int(time.time())),
                    "aid": "1988",
                    "app_language": "en-US",
                    "app_name": "tiktok_web",
                    "browser_language": "en-US",
                    "browser_name": "Mozilla",
                    "browser_online": "true",
                    "browser_platform": "Win32",
                    "browser_version": (
                        "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "channel": "tiktok_web",
                    "cookie_enabled": "true",
                    "count": str(min(12, DISCOVER_RESULTS_PER_QUERY)),
                    "cursor": "0",
                    "device_id": _rand_device_id(),
                    "device_platform": "web_pc",
                    "focus_state": "true",
                    "from_page": "search",
                    "history_len": "3",
                    "is_fullscreen": "false",
                    "is_page_visible": "true",
                    "keyword": q,
                    "offset": "0",
                    "os": "windows",
                    "priority_region": "",
                    "referer": "",
                    "region": "US",
                    "screen_height": "1080",
                    "screen_width": "1920",
                    "tz_name": "America/New_York",
                    "user_is_login": "true",
                    "verifyFp": verify,
                    "webcast_language": "en-US",
                }
                if ms:
                    params["msToken"] = str(ms)

                log.info(
                    "TT keyword search q=%r logged_in=1 sid_tt=%s msToken=%s ttwid=%s",
                    q,
                    bool(jar.get("sid_tt") or jar.get("sessionid")),
                    has_ms or bool(ms),
                    has_ttwid or bool(jar.get("ttwid")),
                )
                resp = client.get(
                    "https://www.tiktok.com/api/search/item/full/",
                    params=params,
                    headers={
                        "Accept": "*/*",
                        "Referer": referer,
                        "Sec-Fetch-Dest": "empty",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "same-origin",
                        "Cookie": cookie_header(jar),
                    },
                )
                jar = _merge_set_cookie(jar, resp)

                if resp.status_code == 429:
                    raise RuntimeError(f"TikTok rate limited (429). {_debug_body(resp)}")
                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        "TikTok auth failed — refresh TIKTOK_SESSIONID / cookies in .env. "
                        f"{_debug_body(resp)}"
                    )
                resp.raise_for_status()

                body = (resp.text or "").strip()
                if not body:
                    raise RuntimeError(
                        "TikTok returned empty body for item/full "
                        f"(cookies present but rejected). {_debug_body(resp)} "
                        "Refresh sid_tt + msToken + ttwid from a logged-in browser."
                    )
                try:
                    payload = resp.json()
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "TikTok returned non-JSON for item/full. "
                        f"{_debug_body(resp)}"
                    ) from exc

                if not isinstance(payload, dict):
                    raise RuntimeError(
                        f"TikTok search unexpected payload type={type(payload).__name__}"
                    )

                items = parse_item_list(payload, q)
                if items:
                    return items

                raise RuntimeError(
                    "TikTok search JSON had no item_list. "
                    f"status_code={payload.get('status_code')} "
                    f"keys={list(payload.keys())[:12]} {_debug_body(resp)}"
                )
        finally:
            _TT_LIMITER.mark()


def search_keyword_safe(query: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    return safe_search("tiktok", search_keyword, query)


def _match_url(url: str) -> bool:
    from platforms.registry import TIKTOK_URL_RE

    return bool(TIKTOK_URL_RE.search((url or "").strip()))


register(
    PlatformSpec(
        name="tiktok",
        label="TikTok",
        search=search_keyword_safe,
        url_match=_match_url,
    )
)
