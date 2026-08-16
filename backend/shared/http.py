"""Shared HTTP helpers for browser-like requests."""

from __future__ import annotations

from typing import Optional

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def httpx_client(
    *,
    timeout: float = 45.0,
    headers: Optional[dict[str, str]] = None,
    **kwargs,
) -> httpx.Client:
    base = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        base.update(headers)
    return httpx.Client(follow_redirects=True, timeout=timeout, headers=base, **kwargs)
