"""Discover (+ shared) knobs. Env overrides where noted."""

from __future__ import annotations

import os

from config.env import env_float, env_int

# Result size
DISCOVER_TOP_N = env_int("DISCOVER_TOP_N", 10)
DISCOVER_MAX_QUERIES = env_int("DISCOVER_MAX_QUERIES", 5)
DISCOVER_RESULTS_PER_QUERY = env_int("DISCOVER_RESULTS_PER_QUERY", 24)

# Instagram rate limit (serialized on purpose)
DISCOVER_IG_MIN_DELAY_SEC = env_float("DISCOVER_IG_MIN_DELAY_SEC", 2.0)
DISCOVER_IG_MAX_DELAY_SEC = env_float("DISCOVER_IG_MAX_DELAY_SEC", 5.0)

# TikTok rate limit (separate from Instagram; serialized)
DISCOVER_TT_MIN_DELAY_SEC = env_float("DISCOVER_TT_MIN_DELAY_SEC", 2.0)
DISCOVER_TT_MAX_DELAY_SEC = env_float("DISCOVER_TT_MAX_DELAY_SEC", 5.0)

# Discover job pool (separate from hook jobs)
DISCOVER_MAX_CONCURRENT_JOBS = env_int("DISCOVER_MAX_CONCURRENT_JOBS", 2)
DISCOVER_MAX_QUEUE_SIZE = env_int("DISCOVER_MAX_QUEUE_SIZE", 10)

# Trending score: engagement weight vs recency
TREND_VIEWS_WEIGHT = min(1.0, env_float("TREND_VIEWS_WEIGHT", 0.65))
TREND_RECENCY_HALF_LIFE_DAYS = env_float("TREND_RECENCY_HALF_LIFE_DAYS", 7.0, minimum=0.1)

DISCOVER_LLM_MODEL = (os.getenv("DISCOVER_LLM_MODEL") or "gpt-4o-mini").strip()

# Scrape: auto = Firecrawl when FIRECRAWL_API_KEY set, else httpx; force with firecrawl|httpx
_raw_scrape_backend = (os.getenv("DISCOVER_SCRAPE_BACKEND") or "auto").strip().lower()
DISCOVER_SCRAPE_BACKEND = (
    _raw_scrape_backend if _raw_scrape_backend in ("auto", "firecrawl", "httpx") else "auto"
)
FIRECRAWL_API_KEY = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
DISCOVER_SCRAPE_MAX_CHARS = env_int("DISCOVER_SCRAPE_MAX_CHARS", 12_000, minimum=500)
DISCOVER_SCRAPE_TIMEOUT_SEC = env_float("DISCOVER_SCRAPE_TIMEOUT_SEC", 45.0, minimum=5.0)

# GraphQL (captured from browser; may rotate)
IG_KEYWORD_DOC_ID = (os.getenv("IG_KEYWORD_DOC_ID") or "37324993597144881").strip()
IG_KEYWORD_FRIENDLY_NAME = "PolarisKeywordSearchExplorePageRelayQuery"
IG_WEB_APP_ID = (os.getenv("IG_WEB_APP_ID") or "936619743392459").strip()
