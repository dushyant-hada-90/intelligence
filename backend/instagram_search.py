"""Instagram web keyword SERP via browser-equivalent GraphQL (rate-limited)."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import uuid
from typing import Any, Optional
from urllib.parse import quote

import httpx

from platforms import PlatformSpec, engagement_from_counts, register
from tunables import (
    DISCOVER_IG_MAX_DELAY_SEC,
    DISCOVER_IG_MIN_DELAY_SEC,
    DISCOVER_RESULTS_PER_QUERY,
    IG_KEYWORD_DOC_ID,
    IG_KEYWORD_FRIENDLY_NAME,
    IG_WEB_APP_ID,
)

log = logging.getLogger("ig-search")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Serialize all IG network work (max concurrent = 1 by design).
_IG_LOCK = threading.Lock()
_LAST_IG_CALL_AT = 0.0
_BOOTSTRAP: dict[str, Any] = {"fb_dtsg": None, "lsd": None, "csrftoken": None}
_BOOTSTRAP_LOCK = threading.Lock()


def _session_id() -> str:
    raw = (os.getenv("INSTAGRAM_SESSIONID") or "").strip().strip('"').strip("'")
    if not raw:
        raise RuntimeError("INSTAGRAM_SESSIONID is required for Instagram keyword search")
    if raw.lower().startswith("sessionid="):
        raw = raw.split("=", 1)[1].strip()
    return raw


def _cookie_header() -> str:
    parts = [f"sessionid={_session_id()}"]
    ds = (os.getenv("INSTAGRAM_DS_USER_ID") or "").strip()
    csrf = (os.getenv("INSTAGRAM_CSRFTOKEN") or "").strip()
    if ds:
        parts.append(f"ds_user_id={ds}")
    if csrf:
        parts.append(f"csrftoken={csrf}")
    return "; ".join(parts)


def _wait_before_ig_call() -> None:
    """Caller must hold _IG_LOCK. Enforce jittered gap since last IG call."""
    global _LAST_IG_CALL_AT
    lo = min(DISCOVER_IG_MIN_DELAY_SEC, DISCOVER_IG_MAX_DELAY_SEC)
    hi = max(DISCOVER_IG_MIN_DELAY_SEC, DISCOVER_IG_MAX_DELAY_SEC)
    delay = random.uniform(lo, hi)
    now = time.monotonic()
    wait = (_LAST_IG_CALL_AT + delay) - now
    if wait > 0:
        time.sleep(wait)


def _mark_ig_call() -> None:
    global _LAST_IG_CALL_AT
    _LAST_IG_CALL_AT = time.monotonic()


def _extract_token(html: str, patterns: list[str]) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def _update_csrf_from_response(resp: httpx.Response) -> None:
    csrf = resp.cookies.get("csrftoken")
    if csrf:
        with _BOOTSTRAP_LOCK:
            _BOOTSTRAP["csrftoken"] = csrf


def _bootstrap_tokens(client: httpx.Client, query: str) -> dict[str, str]:
    """Load keyword page once to get fb_dtsg / lsd / csrftoken."""
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAP.get("fb_dtsg") and _BOOTSTRAP.get("lsd"):
            return {
                "fb_dtsg": _BOOTSTRAP["fb_dtsg"],
                "lsd": _BOOTSTRAP["lsd"],
                "csrftoken": _BOOTSTRAP.get("csrftoken")
                or (os.getenv("INSTAGRAM_CSRFTOKEN") or "missing"),
            }

    url = f"https://www.instagram.com/explore/search/keyword/?q={quote(query)}"
    log.info("bootstrapping IG tokens via keyword page")
    resp = client.get(url)
    resp.raise_for_status()
    _update_csrf_from_response(resp)
    html = resp.text

    fb_dtsg = _extract_token(
        html,
        [
            r'"DTSGInitialData",\[\],\{"token":"([^"]+)"',
            r'"dtsg":\{"token":"([^"]+)"',
            r'name="fb_dtsg"\s+value="([^"]+)"',
            r'"f":"([^"]+)"[^}]*"b":"dtsg"',
        ],
    )
    lsd = _extract_token(
        html,
        [
            r'"LSD",\[\],\{"token":"([^"]+)"',
            r'name="lsd"\s+value="([^"]+)"',
            r'"lsd"\s*:\s*"([^"]+)"',
        ],
    )
    if not fb_dtsg or not lsd:
        raise RuntimeError(
            "Could not bootstrap Instagram fb_dtsg/lsd from keyword page "
            "(session may be expired or challenged)"
        )

    csrf = (
        resp.cookies.get("csrftoken")
        or (os.getenv("INSTAGRAM_CSRFTOKEN") or "").strip()
        or "missing"
    )
    with _BOOTSTRAP_LOCK:
        _BOOTSTRAP["fb_dtsg"] = fb_dtsg
        _BOOTSTRAP["lsd"] = lsd
        _BOOTSTRAP["csrftoken"] = csrf
    return {"fb_dtsg": fb_dtsg, "lsd": lsd, "csrftoken": csrf}


def _client() -> httpx.Client:
    csrf_env = (os.getenv("INSTAGRAM_CSRFTOKEN") or "").strip()
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.instagram.com",
        "Referer": "https://www.instagram.com/",
        "Cookie": _cookie_header(),
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if csrf_env:
        headers["X-CSRFToken"] = csrf_env
    return httpx.Client(follow_redirects=True, timeout=45.0, headers=headers)


def _is_reel(item: dict[str, Any]) -> bool:
    if item.get("media_type") == 2:
        return True
    if item.get("video_versions"):
        return True
    return False


def _thumb_url(item: dict[str, Any]) -> Optional[str]:
    cands = ((item.get("image_versions2") or {}).get("candidates")) or []
    if not cands:
        return None
    return cands[0].get("url")


def _normalize_item(item: dict[str, Any], query: str) -> Optional[dict[str, Any]]:
    if not _is_reel(item):
        return None
    code = item.get("code")
    if not code:
        return None
    user = item.get("user") or {}
    view_count = item.get("view_count")
    like_count = item.get("like_count")
    engagement, engagement_source = engagement_from_counts(
        int(view_count) if view_count is not None else None,
        int(like_count) if like_count is not None else None,
    )

    caption_obj = item.get("caption")
    if isinstance(caption_obj, dict):
        caption = caption_obj.get("text") or ""
    else:
        caption = str(caption_obj or "")

    return {
        "id": str(item.get("id") or item.get("pk") or code),
        "pk": str(item.get("pk") or ""),
        "code": code,
        "platform": "instagram",
        "url": f"https://www.instagram.com/reel/{code}/",
        "caption": caption,
        "username": user.get("username") or "",
        "taken_at": int(item.get("taken_at") or 0),
        "view_count": int(view_count) if view_count is not None else None,
        "like_count": int(like_count) if like_count is not None else None,
        "comment_count": int(item["comment_count"])
        if item.get("comment_count") is not None
        else None,
        "engagement": engagement,
        "engagement_source": engagement_source,
        "thumbnail_url": _thumb_url(item),
        "query": query,
    }


def parse_serp_media(payload: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Extract reel items from PolarisKeywordSearchExplorePageRelayQuery JSON."""
    root = (payload.get("data") or {}).get("xdt_fbsearch__top_serp_graphql") or {}
    edges = root.get("edges") or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for edge in edges:
        node = (edge or {}).get("node") or {}
        if node.get("__typename") != "XDTTopSerpMediaGridUnit":
            continue
        for item in node.get("items") or []:
            if not isinstance(item, dict):
                continue
            norm = _normalize_item(item, query)
            if not norm:
                continue
            key = norm["code"]
            if key in seen:
                continue
            seen.add(key)
            out.append(norm)
            if len(out) >= DISCOVER_RESULTS_PER_QUERY:
                return out
    return out


def search_keyword(query: str) -> list[dict[str, Any]]:
    """Run one keyword SERP request; raises on hard failures."""
    q = (query or "").strip()
    if not q:
        return []

    session_uuid = str(uuid.uuid4())
    variables = {
        "query": q,
        "search_session_id": session_uuid,
        "serp_session_id": session_uuid,
    }

    with _IG_LOCK:
        _wait_before_ig_call()
        try:
            with _client() as client:
                tokens = _bootstrap_tokens(client, q)
                csrf = tokens["csrftoken"]
                referer = f"https://www.instagram.com/explore/search/keyword/?q={quote(q)}"
                form = {
                    "fb_dtsg": tokens["fb_dtsg"],
                    "lsd": tokens["lsd"],
                    "jazoest": "26745",
                    "__comet_req": "7",
                    "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": IG_KEYWORD_FRIENDLY_NAME,
                    "server_timestamps": "true",
                    "variables": json.dumps(variables, separators=(",", ":")),
                    "doc_id": IG_KEYWORD_DOC_ID,
                }
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": referer,
                    "X-CSRFToken": csrf,
                    "X-FB-LSD": tokens["lsd"],
                    "X-IG-App-ID": IG_WEB_APP_ID,
                    "X-FB-Friendly-Name": IG_KEYWORD_FRIENDLY_NAME,
                }
                log.info("IG keyword search q=%r", q)
                resp = client.post(
                    "https://www.instagram.com/api/graphql",
                    data=form,
                    headers=headers,
                )
                _update_csrf_from_response(resp)
                if resp.status_code in (401, 403):
                    with _BOOTSTRAP_LOCK:
                        _BOOTSTRAP["fb_dtsg"] = None
                        _BOOTSTRAP["lsd"] = None
                    raise RuntimeError(
                        f"Instagram auth failed ({resp.status_code}) — refresh INSTAGRAM_SESSIONID"
                    )
                if resp.status_code == 429:
                    raise RuntimeError("Instagram rate limited (429)")
                resp.raise_for_status()
                try:
                    payload = resp.json()
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "Instagram returned non-JSON for keyword search"
                    ) from exc

                if payload.get("errors"):
                    raise RuntimeError(
                        f"Instagram GraphQL errors: {payload.get('errors')}"
                    )

                return parse_serp_media(payload, q)
        finally:
            _mark_ig_call()


def search_keyword_safe(query: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Soft-fail wrapper: returns (reels, warning_or_None)."""
    try:
        return search_keyword(query), None
    except Exception as exc:
        log.warning("keyword search failed q=%r: %s", query, exc)
        return [], f"instagram query={query!r}: {exc}"


def _match_url(url: str) -> bool:
    from platforms import INSTAGRAM_URL_RE

    return bool(INSTAGRAM_URL_RE.search((url or "").strip()))


register(
    PlatformSpec(
        name="instagram",
        label="Instagram",
        search=search_keyword_safe,
        url_match=_match_url,
    )
)
