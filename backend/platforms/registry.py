"""Platform registry for discover search + hook URL/download routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse

# Search: (query) -> (reels, warning_or_None)
SearchFn = Callable[[str], tuple[list[dict[str, Any]], Optional[str]]]
MatchFn = Callable[[str], bool]

INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
TIKTOK_URL_RE = re.compile(
    r"https?://(?:(?:www|vm|vt)\.)?tiktok\.com/",
    re.IGNORECASE,
)
TIKTOK_VIDEO_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlatformSpec:
    name: str
    label: str
    search: SearchFn
    url_match: MatchFn


_REGISTRY: dict[str, PlatformSpec] = {}


def register(spec: PlatformSpec) -> None:
    _REGISTRY[spec.name] = spec


def ensure_loaded() -> None:
    """Import adapters so they self-register (idempotent)."""
    import platforms.instagram.search  # noqa: F401
    import platforms.tiktok.search  # noqa: F401


def known_platforms() -> list[str]:
    ensure_loaded()
    return sorted(_REGISTRY.keys())


def platform_specs() -> list[dict[str, str]]:
    ensure_loaded()
    return [
        {"name": s.name, "label": s.label}
        for s in sorted(_REGISTRY.values(), key=lambda x: x.name)
    ]


def get_search(name: str) -> SearchFn:
    ensure_loaded()
    spec = _REGISTRY.get(name)
    if spec is None:
        raise KeyError(name)
    return spec.search


def validate_platforms(platforms: Optional[list[str]]) -> list[str]:
    """Normalize + validate; default Instagram for backward compatibility."""
    ensure_loaded()
    if platforms is None or len(platforms) == 0:
        return ["instagram"]
    cleaned: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for raw in platforms:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name not in _REGISTRY:
            unknown.append(name)
            continue
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
    if unknown:
        allowed = ", ".join(known_platforms())
        bad = ", ".join(unknown)
        raise ValueError(f"Unknown platforms: {bad}. Allowed: {allowed}")
    if not cleaned:
        return ["instagram"]
    return cleaned


def detect_platform(url: str) -> Optional[str]:
    """Return platform name for a video URL, or None."""
    ensure_loaded()
    cleaned = (url or "").strip()
    for spec in _REGISTRY.values():
        if spec.url_match(cleaned):
            return spec.name
    if INSTAGRAM_URL_RE.search(cleaned):
        return "instagram"
    if TIKTOK_URL_RE.search(cleaned):
        return "tiktok"
    return None


def canonicalize_video_url(url: str) -> str:
    """Strip tracking query/fragment for stable download URLs."""
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        return cleaned
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def engagement_from_counts(
    view_count: Optional[int], like_count: Optional[int]
) -> tuple[int, str]:
    if view_count is not None:
        return int(view_count), "view_count"
    if like_count is not None:
        return int(like_count), "like_count"
    return 0, "none"


def reel_dedupe_key(reel: dict[str, Any]) -> str:
    platform = str(reel.get("platform") or "")
    rid = str(reel.get("id") or reel.get("code") or "")
    return f"{platform}:{rid}"
