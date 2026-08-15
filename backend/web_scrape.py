"""Fetch a business landing page and extract readable text for query generation."""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from tunables import DISCOVER_SCRAPE_MAX_CHARS, DISCOVER_SCRAPE_TIMEOUT_SEC

log = logging.getLogger("web-scrape")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"}
_SCRAPE_RETRIES = 3


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


def _fetch_html(url: str, headers: dict[str, str], timeout: float) -> tuple[str, str]:
    """GET with retries on transient DNS/connect errors."""
    last_exc: Exception | None = None
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
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


def scrape_landing_page(url: str) -> dict[str, Any]:
    """Return title, description, headings, and capped body text."""
    cleaned = _normalize_url(url)
    timeout = DISCOVER_SCRAPE_TIMEOUT_SEC
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    log.info("scraping %s", cleaned)
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
    body_text = _clean_text(main.get_text("\n", strip=True) if main else "")
    if len(body_text) > DISCOVER_SCRAPE_MAX_CHARS:
        body_text = body_text[: DISCOVER_SCRAPE_MAX_CHARS].rsplit(" ", 1)[0] + "…"

    return {
        "url": cleaned,
        "final_url": final_url,
        "title": title,
        "description": description,
        "headings": headings,
        "text": body_text,
        "text_chars": len(body_text),
    }
