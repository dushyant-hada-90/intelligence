"""Soft-fail wrapper for platform keyword search."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("platform-search")

SearchFn = Callable[[str], list[dict[str, Any]]]


def safe_search(
    platform: str,
    search_fn: SearchFn,
    query: str,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        return search_fn(query), None
    except Exception as exc:
        log.warning("%s keyword search failed q=%r: %s", platform, query, exc)
        return [], f"{platform} query={query!r}: {exc}"
