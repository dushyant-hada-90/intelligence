"""Orchestrate scrape → LLM queries → IG search → score → top N."""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from instagram_search import search_keyword_safe
from query_gen import generate_tags_and_queries
from tunables import (
    DISCOVER_TOP_N,
    TREND_RECENCY_HALF_LIFE_DAYS,
    TREND_VIEWS_WEIGHT,
)
from web_scrape import scrape_landing_page

log = logging.getLogger("discover-pipeline")


def trend_score(*, engagement: int, taken_at: int, now: float | None = None) -> float:
    """Deterministic combined engagement + recency score."""
    ts = now if now is not None else time.time()
    age_days = max(0.0, (ts - float(taken_at or 0)) / 86400.0)
    w = TREND_VIEWS_WEIGHT
    eng = math.log1p(max(0, int(engagement)))
    rec = math.exp(-age_days / TREND_RECENCY_HALF_LIFE_DAYS)
    return (w * eng) + ((1.0 - w) * rec)


def rank_reels(reels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    ranked: list[dict[str, Any]] = []
    for r in reels:
        item = dict(r)
        score = trend_score(
            engagement=int(item.get("engagement") or 0),
            taken_at=int(item.get("taken_at") or 0),
            now=now,
        )
        item["score"] = round(score, 8)
        ranked.append(item)
    ranked.sort(
        key=lambda x: (-x["score"], -(x.get("taken_at") or 0), str(x.get("id") or ""))
    )
    return ranked[:DISCOVER_TOP_N]


def run_discover(url: str) -> dict[str, Any]:
    website = scrape_landing_page(url)
    gen = generate_tags_and_queries(website)
    tags = gen.get("tags") or []
    queries = gen.get("queries") or []
    llm_usage = gen.get("usage")
    cost_usd = float(gen.get("cost_usd") or 0.0)

    warnings: list[str] = []
    by_code: dict[str, dict[str, Any]] = {}

    if not queries:
        warnings.append("LLM returned no search queries")

    for q in queries:
        reels, warn = search_keyword_safe(q)
        if warn:
            warnings.append(warn)
        for reel in reels:
            code = reel["code"]
            prev = by_code.get(code)
            if prev is None:
                by_code[code] = reel
            else:
                # Prefer higher engagement; merge source queries
                if (reel.get("engagement") or 0) > (prev.get("engagement") or 0):
                    merged = dict(reel)
                else:
                    merged = dict(prev)
                src = sorted(
                    {
                        *(str(x) for x in (prev.get("queries") or [prev.get("query")])),
                        *(str(x) for x in (reel.get("queries") or [reel.get("query")])),
                    }
                )
                merged["query"] = src[0] if len(src) == 1 else prev.get("query")
                merged["queries"] = src
                by_code[code] = merged

    top = rank_reels(list(by_code.values()))
    log.info(
        "discover done url=%s queries=%s unique=%s top=%s warnings=%s",
        website.get("url"),
        len(queries),
        len(by_code),
        len(top),
        len(warnings),
    )
    return {
        "website": {
            "url": website.get("url"),
            "final_url": website.get("final_url"),
            "title": website.get("title"),
            "description": website.get("description"),
            "headings": website.get("headings"),
            "text_chars": website.get("text_chars"),
        },
        "tags": tags,
        "queries": queries,
        "reels": top,
        "warnings": warnings,
        "usage": {"llm": llm_usage},
        "cost_usd": cost_usd,
    }
