"""Discover (+ shared) knobs. Env overrides where noted."""

from __future__ import annotations

import os


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


# Result size
DISCOVER_TOP_N = _env_int("DISCOVER_TOP_N", 4)
DISCOVER_MAX_QUERIES = _env_int("DISCOVER_MAX_QUERIES", 5)
DISCOVER_RESULTS_PER_QUERY = _env_int("DISCOVER_RESULTS_PER_QUERY", 24)

# Instagram rate limit (serialized on purpose)
DISCOVER_IG_MIN_DELAY_SEC = _env_float("DISCOVER_IG_MIN_DELAY_SEC", 2.0)
DISCOVER_IG_MAX_DELAY_SEC = _env_float("DISCOVER_IG_MAX_DELAY_SEC", 5.0)
DISCOVER_IG_MAX_CONCURRENT = 1

# TikTok rate limit (separate from Instagram; serialized)
DISCOVER_TT_MIN_DELAY_SEC = _env_float("DISCOVER_TT_MIN_DELAY_SEC", 2.0)
DISCOVER_TT_MAX_DELAY_SEC = _env_float("DISCOVER_TT_MAX_DELAY_SEC", 5.0)
DISCOVER_TT_MAX_CONCURRENT = 1

# Discover job pool (separate from hook jobs)
DISCOVER_MAX_CONCURRENT_JOBS = _env_int("DISCOVER_MAX_CONCURRENT_JOBS", 2)
DISCOVER_MAX_QUEUE_SIZE = _env_int("DISCOVER_MAX_QUEUE_SIZE", 10)

# Trending score: engagement weight vs recency
TREND_VIEWS_WEIGHT = min(1.0, _env_float("TREND_VIEWS_WEIGHT", 0.65))
TREND_RECENCY_HALF_LIFE_DAYS = _env_float("TREND_RECENCY_HALF_LIFE_DAYS", 7.0, minimum=0.1)

DISCOVER_LLM_MODEL = (os.getenv("DISCOVER_LLM_MODEL") or "gpt-4o-mini").strip()

# Scrape
DISCOVER_SCRAPE_MAX_CHARS = _env_int("DISCOVER_SCRAPE_MAX_CHARS", 12_000, minimum=500)
DISCOVER_SCRAPE_TIMEOUT_SEC = _env_float("DISCOVER_SCRAPE_TIMEOUT_SEC", 20.0, minimum=5.0)

# GraphQL (captured from browser; may rotate)
IG_KEYWORD_DOC_ID = (os.getenv("IG_KEYWORD_DOC_ID") or "37324993597144881").strip()
IG_KEYWORD_FRIENDLY_NAME = "PolarisKeywordSearchExplorePageRelayQuery"
IG_WEB_APP_ID = (os.getenv("IG_WEB_APP_ID") or "936619743392459").strip()
