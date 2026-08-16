"""Fetch a business landing page and extract readable text for query generation."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from config.tunables import (
    DISCOVER_SCRAPE_BACKEND,
    DISCOVER_SCRAPE_MAX_CHARS,
    DISCOVER_SCRAPE_TIMEOUT_SEC,
    FIRECRAWL_API_KEY,
)
from shared.http import USER_AGENT, httpx_client

log = logging.getLogger("web-scrape")

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"}
_SCRAPE_RETRIES = 3
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)


def _normalize_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("Website URL is required")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("URL is missing a host")
    return cleaned


def _meta_content(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str:
    if name:
        tag = soup.find("meta", attrs={"name": name})
    else:
        tag = soup.find("meta", attrs={"property": prop})
    if not tag:
        return ""
    return (tag.get("content") or "").strip()


def _clean_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cap_text(body_text: str) -> str:
    if len(body_text) > DISCOVER_SCRAPE_MAX_CHARS:
        return body_text[: DISCOVER_SCRAPE_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return body_text


def _doc_get(doc: Any, key: str, default: Any = None) -> Any:
    """Read Firecrawl fields from attribute or dict-style responses."""
    if doc is None:
        return default
    if isinstance(doc, dict):
        return doc.get(key, default)
    val = getattr(doc, key, None)
    if val is not None:
        return val
    if hasattr(doc, "get"):
        try:
            return doc.get(key, default)
        except Exception:
            pass
    return default


def _metadata_dict(doc: Any) -> dict[str, Any]:
    meta = _doc_get(doc, "metadata") or {}
    if hasattr(meta, "model_dump"):
        try:
            return dict(meta.model_dump(exclude_none=True))
        except Exception:
            pass
    if hasattr(meta, "dict"):
        try:
            return dict(meta.dict(exclude_none=True))
        except Exception:
            pass
    if isinstance(meta, dict):
        return meta
    out: dict[str, Any] = {}
    for key in (
        "title",
        "description",
        "og_title",
        "og_description",
        "ogTitle",
        "ogDescription",
        "source_url",
        "sourceURL",
        "url",
    ):
        val = getattr(meta, key, None)
        if val is not None:
            out[key] = val
    return out


def _headings_from_markdown(markdown: str) -> list[str]:
    headings: list[str] = []
    for match in _HEADING_RE.finditer(markdown or ""):
        t = match.group(1).strip()
        if t and t not in headings:
            headings.append(t)
        if len(headings) >= 20:
            break
    return headings


def _fetch_html(url: str, headers: dict[str, str], timeout: float) -> tuple[str, str]:
    """GET with retries on transient DNS/connect errors."""
    last_exc: Exception | None = None
    with httpx_client(timeout=timeout, headers=headers) as client:
        for attempt in range(1, _SCRAPE_RETRIES + 1):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text, str(resp.url)
            except httpx.ConnectError as exc:
                last_exc = exc
                log.warning(
                    "scrape connect failed attempt=%s/%s url=%s err=%s",
                    attempt,
                    _SCRAPE_RETRIES,
                    url,
                    exc,
                )
                if attempt < _SCRAPE_RETRIES:
                    time.sleep(0.8 * attempt)
            except httpx.TimeoutException as exc:
                last_exc = exc
                log.warning(
                    "scrape timeout attempt=%s/%s url=%s",
                    attempt,
                    _SCRAPE_RETRIES,
                    url,
                )
                if attempt < _SCRAPE_RETRIES:
                    time.sleep(0.8 * attempt)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Website returned HTTP {exc.response.status_code} for {url}"
                ) from exc

    host = urlparse(url).netloc
    raise RuntimeError(
        f"Could not reach {host} (DNS/network). "
        f"Check the URL and your internet connection, then retry. Detail: {last_exc}"
    )


def _scrape_httpx(cleaned: str) -> dict[str, Any]:
    """Static HTML scrape via httpx + BeautifulSoup."""
    timeout = DISCOVER_SCRAPE_TIMEOUT_SEC
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    log.info("scraping httpx %s", cleaned)
    html, final_url = _fetch_html(cleaned, headers, timeout)

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_SKIP_TAGS):
        tag.decompose()

    title = (soup.title.string or "").strip() if soup.title else ""
    if not title:
        title = _meta_content(soup, prop="og:title")

    description = (
        _meta_content(soup, name="description")
        or _meta_content(soup, prop="og:description")
        or _meta_content(soup, name="twitter:description")
    )

    headings: list[str] = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = h.get_text(" ", strip=True)
        if t and t not in headings:
            headings.append(t)
        if len(headings) >= 20:
            break

    main = soup.find("main") or soup.find("article") or soup.body
    body_text = _cap_text(_clean_text(main.get_text("\n", strip=True) if main else ""))

    return {
        "url": cleaned,
        "final_url": final_url,
        "title": title,
        "description": description,
        "headings": headings,
        "text": body_text,
        "text_chars": len(body_text),
    }


def _scrape_firecrawl(cleaned: str, api_key: str) -> dict[str, Any]:
    """JS-rendered scrape via Firecrawl → markdown + metadata."""
    from firecrawl import Firecrawl

    timeout_ms = int(DISCOVER_SCRAPE_TIMEOUT_SEC * 1000)
    log.info("scraping firecrawl %s", cleaned)
    client = Firecrawl(api_key=api_key)
    doc = client.scrape(
        cleaned,
        formats=["markdown"],
        only_main_content=True,
        timeout=timeout_ms,
    )

    markdown = (_doc_get(doc, "markdown") or "").strip()
    if not markdown:
        raise RuntimeError("Firecrawl returned empty markdown")

    meta = _metadata_dict(doc)
    title = str(
        meta.get("title")
        or meta.get("og_title")
        or meta.get("ogTitle")
        or ""
    ).strip()
    description = str(
        meta.get("description")
        or meta.get("og_description")
        or meta.get("ogDescription")
        or ""
    ).strip()
    final_url = str(
        meta.get("source_url")
        or meta.get("sourceURL")
        or meta.get("url")
        or cleaned
    ).strip() or cleaned

    body_text = _cap_text(_clean_text(markdown))
    headings = _headings_from_markdown(markdown)

    return {
        "url": cleaned,
        "final_url": final_url,
        "title": title,
        "description": description,
        "headings": headings,
        "text": body_text,
        "text_chars": len(body_text),
    }


def _use_firecrawl() -> tuple[bool, str]:
    """Return (should_try_firecrawl, api_key)."""
    backend = DISCOVER_SCRAPE_BACKEND
    key = FIRECRAWL_API_KEY or (os.getenv("FIRECRAWL_API_KEY") or "").strip()
    if backend == "httpx":
        return False, ""
    if backend == "firecrawl":
        if not key:
            raise RuntimeError(
                "DISCOVER_SCRAPE_BACKEND=firecrawl requires FIRECRAWL_API_KEY"
            )
        return True, key
    # auto
    return bool(key), key


def scrape_landing_page(url: str) -> dict[str, Any]:
    """Return title, description, headings, and capped body text."""
    cleaned = _normalize_url(url)
    try_fc, api_key = _use_firecrawl()

    if try_fc:
        try:
            return _scrape_firecrawl(cleaned, api_key)
        except Exception as exc:
            if DISCOVER_SCRAPE_BACKEND == "firecrawl":
                raise RuntimeError(f"Firecrawl scrape failed for {cleaned}: {exc}") from exc
            log.warning(
                "firecrawl scrape failed, falling back to httpx url=%s err=%s",
                cleaned,
                exc,
            )

    return _scrape_httpx(cleaned)
