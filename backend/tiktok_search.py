"""TikTok guest keyword SERP via Playwright (capture real signed item/full).

Runs Playwright on a dedicated thread with its own asyncio loop so it does not
conflict with uvicorn. Prefers installed Google Chrome when available (less
flagged than stock Chromium headless).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import random
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

from platforms import PlatformSpec, engagement_from_counts, register
from tunables import (
    DISCOVER_RESULTS_PER_QUERY,
    DISCOVER_TT_MAX_DELAY_SEC,
    DISCOVER_TT_MIN_DELAY_SEC,
)

log = logging.getLogger("tt-search")

os.environ.setdefault("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_SHELL", "0")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

_SEARCH_URL_HINTS = (
    "api/search/item/full",
    "api/search/general/full",
    "api/search/item/",
)

_TT_LOCK = threading.Lock()
_LAST_TT_CALL_AT = 0.0

_WORKER: Optional["_PlaywrightWorker"] = None
_WORKER_LOCK = threading.Lock()


def _wait_before_tt_call() -> None:
    global _LAST_TT_CALL_AT
    lo = min(DISCOVER_TT_MIN_DELAY_SEC, DISCOVER_TT_MAX_DELAY_SEC)
    hi = max(DISCOVER_TT_MIN_DELAY_SEC, DISCOVER_TT_MAX_DELAY_SEC)
    delay = random.uniform(lo, hi)
    now = time.monotonic()
    wait = (_LAST_TT_CALL_AT + delay) - now
    if wait > 0:
        time.sleep(wait)


def _mark_tt_call() -> None:
    global _LAST_TT_CALL_AT
    _LAST_TT_CALL_AT = time.monotonic()


def _playwright_enabled() -> bool:
    raw = (os.getenv("TIKTOK_SEARCH_PLAYWRIGHT") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _headless() -> bool:
    # Default headful — TikTok often empties item/full under automation headless.
    raw = (os.getenv("TIKTOK_PLAYWRIGHT_HEADLESS") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _chrome_channel() -> Optional[str]:
    """Prefer real Chrome/Edge when installed (set TIKTOK_PLAYWRIGHT_CHANNEL=chromium to force)."""
    raw = (os.getenv("TIKTOK_PLAYWRIGHT_CHANNEL") or "chrome").strip().lower()
    if raw in {"", "auto", "chrome"}:
        return "chrome"
    if raw in {"msedge", "edge"}:
        return "msedge"
    if raw in {"chromium", "none", "0"}:
        return None
    return raw


def _is_search_api(url: str) -> bool:
    u = url or ""
    return any(h in u for h in _SEARCH_URL_HINTS)


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
    items = payload.get("item_list") or payload.get("data") or []
    if isinstance(items, dict):
        items = items.get("item_list") or items.get("videos") or []
    if not isinstance(items, list):
        items = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        # general/full sometimes nests under item / aweme_info
        if "aweme_info" in item and isinstance(item["aweme_info"], dict):
            item = item["aweme_info"]
        elif "item" in item and isinstance(item["item"], dict):
            item = item["item"]
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


def _challenge_hint(html: str) -> str:
    low = (html or "").lower()
    if "captcha" in low or "verify" in low and "robot" in low:
        return "captcha/challenge page"
    if "access denied" in low:
        return "access denied"
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    title = re.sub(r"\s+", " ", (title_m.group(1) if title_m else "")).strip()[:80]
    return f"title={title!r}" if title else "no challenge markers"


class _PlaywrightWorker:
    """Dedicated thread + asyncio loop for Playwright (avoids uvicorn conflict)."""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._thread_main, name="tt-playwright", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=90):
            raise RuntimeError(
                "Playwright worker failed to start in time. "
                "Run: playwright install chromium"
            )
        if self._error is not None:
            raise RuntimeError(f"Playwright worker failed: {self._error}") from self._error

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._amain())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            log.exception("Playwright worker crashed")

    async def _launch_browser(self, p):
        channel = _chrome_channel()
        headless = _headless()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
        ]
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "args": args,
        }
        if channel:
            launch_kwargs["channel"] = channel
        log.info(
            "starting Playwright for TikTok search headless=%s channel=%s",
            headless,
            channel or "chromium",
        )
        try:
            return await p.chromium.launch(**launch_kwargs)
        except Exception as exc:
            if channel:
                log.warning(
                    "channel=%s launch failed (%s); falling back to bundled Chromium",
                    channel,
                    exc,
                )
                launch_kwargs.pop("channel", None)
                return await p.chromium.launch(**launch_kwargs)
            raise

    async def _amain(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            self._error = RuntimeError(
                "Playwright is required for TikTok search. "
                "Run: pip install playwright && playwright install chromium"
            )
            self._ready.set()
            raise self._error from exc

        async with async_playwright() as p:
            try:
                browser = await self._launch_browser(p)
            except Exception as exc:
                self._error = RuntimeError(
                    f"Chromium/Chrome launch failed: {exc}. "
                    "Install Google Chrome or run: playwright install chromium"
                )
                self._ready.set()
                raise self._error from exc

            context = await browser.new_context(
                user_agent=_USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1365, "height": 900},
                color_scheme="light",
                has_touch=False,
                java_script_enabled=True,
            )
            await context.add_init_script(_STEALTH_INIT)
            self._ready.set()
            try:
                # Warm homepage once so cookies exist before search pages.
                try:
                    warm = await context.new_page()
                    await warm.goto(
                        "https://www.tiktok.com/",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await warm.wait_for_timeout(1500)
                    await warm.close()
                except Exception as exc:
                    log.warning("TikTok homepage warmup failed: %s", exc)

                while True:
                    job = self._jobs.get()
                    if job is None:
                        break
                    fut, query = job
                    try:
                        result = await self._search(context, query)
                        fut.set_result(result)
                    except Exception as exc:
                        fut.set_exception(exc)
            finally:
                await context.close()
                await browser.close()

    async def _search(self, context, query: str) -> list[dict[str, Any]]:
        page = await context.new_page()
        captured: dict[str, Any] = {
            "payload": None,
            "status": None,
            "url": None,
            "empty_hits": 0,
            "seen": [],
        }

        async def ingest_response(response) -> None:
            url = response.url or ""
            if not _is_search_api(url):
                return
            captured["seen"].append(f"{response.status}:{url.split('?', 1)[0]}")
            if captured["payload"] is not None:
                return
            captured["status"] = response.status
            captured["url"] = url
            text = ""
            try:
                text = (await response.text()) or ""
            except Exception:
                try:
                    data = await response.json()
                    if isinstance(data, dict) and (
                        data.get("item_list") or data.get("status_code") is not None
                    ):
                        captured["payload"] = data
                        return
                except Exception:
                    return
            if not text.strip():
                captured["empty_hits"] += 1
                log.info(
                    "TT search API empty body status=%s url=%s",
                    response.status,
                    url.split("?", 1)[0],
                )
                return
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                log.info(
                    "TT search API non-JSON status=%s bytes=%s",
                    response.status,
                    len(text),
                )
                return
            if isinstance(data, dict):
                captured["payload"] = data

        page.on("response", ingest_response)
        search_url = f"https://www.tiktok.com/search/video?q={quote(query)}"
        log.info("TT Playwright search q=%r", query)
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=90_000)
            # Let client JS boot + fire signed XHR; nudge scroll/tab.
            for _ in range(8):
                if captured["payload"] is not None:
                    break
                await page.wait_for_timeout(800)
                try:
                    await page.mouse.wheel(0, 1200)
                except Exception:
                    pass
            if captured["payload"] is None:
                for sel in (
                    '[data-e2e="search-video"]',
                    'a[href*="/search/video"]',
                    'div[role="tab"]:has-text("Videos")',
                ):
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0:
                            await loc.click(timeout=2000)
                            await page.wait_for_timeout(2000)
                            break
                    except Exception:
                        continue
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and captured["payload"] is None:
                await page.wait_for_timeout(400)

            if isinstance(captured["payload"], dict):
                items = parse_item_list(captured["payload"], query)
                if items:
                    log.info(
                        "TT Playwright search q=%r n=%s http=%s",
                        query,
                        len(items),
                        captured["status"],
                    )
                    return items
                status = captured["payload"].get("status_code")
                raise RuntimeError(
                    f"TikTok search JSON empty item_list "
                    f"(status_code={status}, http={captured['status']})"
                )

            html = await page.content()
            embedded = _extract_embedded_items(html or "", query)
            if embedded:
                log.info("TT Playwright embedded HTML q=%r n=%s", query, len(embedded))
                return embedded

            seen = ", ".join(captured["seen"][:6]) or "none"
            raise RuntimeError(
                "TikTok search did not return usable JSON in browser "
                f"({_challenge_hint(html)}; empty_api_hits={captured['empty_hits']}; "
                f"seen=[{seen}]). Try TIKTOK_PLAYWRIGHT_HEADLESS=0 and install Google Chrome."
            )
        finally:
            await page.close()

    def search(self, query: str, *, timeout: float = 120.0) -> list[dict[str, Any]]:
        box: dict[str, Any] = {}
        done = threading.Event()

        class _BoxFuture:
            def set_result(self, value: Any) -> None:
                box["result"] = value
                done.set()

            def set_exception(self, exc: BaseException) -> None:
                box["error"] = exc
                done.set()

        self._jobs.put((_BoxFuture(), query))
        if not done.wait(timeout=timeout):
            raise RuntimeError(f"TikTok Playwright search timed out after {timeout}s")
        if "error" in box:
            raise box["error"]
        return box["result"]


def _get_worker() -> _PlaywrightWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = _PlaywrightWorker()
        return _WORKER


def search_keyword(query: str) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    with _TT_LOCK:
        _wait_before_tt_call()
        try:
            if not _playwright_enabled():
                raise RuntimeError(
                    "TIKTOK_SEARCH_PLAYWRIGHT=0. TikTok search needs Playwright "
                    "(httpx alone gets empty JSON). Set TIKTOK_SEARCH_PLAYWRIGHT=1."
                )
            return _get_worker().search(q)
        finally:
            _mark_tt_call()


def search_keyword_safe(query: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        return search_keyword(query), None
    except Exception as exc:
        log.warning("tiktok keyword search failed q=%r: %s", query, exc)
        return [], f"tiktok query={query!r}: {exc}"


def _match_url(url: str) -> bool:
    from platforms import TIKTOK_URL_RE

    return bool(TIKTOK_URL_RE.search((url or "").strip()))


register(
    PlatformSpec(
        name="tiktok",
        label="TikTok",
        search=search_keyword_safe,
        url_match=_match_url,
    )
)
