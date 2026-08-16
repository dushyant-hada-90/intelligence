"""Platform registry public API."""

from platforms.registry import (
    INSTAGRAM_URL_RE,
    TIKTOK_URL_RE,
    TIKTOK_VIDEO_RE,
    PlatformSpec,
    canonicalize_video_url,
    detect_platform,
    engagement_from_counts,
    ensure_loaded,
    get_search,
    known_platforms,
    media_key,
    platform_specs,
    reel_dedupe_key,
    register,
    validate_platforms,
)

__all__ = [
    "INSTAGRAM_URL_RE",
    "TIKTOK_URL_RE",
    "TIKTOK_VIDEO_RE",
    "PlatformSpec",
    "canonicalize_video_url",
    "detect_platform",
    "engagement_from_counts",
    "ensure_loaded",
    "get_search",
    "known_platforms",
    "media_key",
    "platform_specs",
    "reel_dedupe_key",
    "register",
    "validate_platforms",
]
