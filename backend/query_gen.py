"""LLM: landing page summary → Instagram keyword search queries + tags."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from openai import OpenAI

from tunables import DISCOVER_LLM_MODEL, DISCOVER_MAX_QUERIES
from usage_costs import cost_from_tokens

log = logging.getLogger("query-gen")

# gpt-4o-mini public list (approx); verify before billing
GPT4O_MINI_INPUT_PER_1M = 0.15
GPT4O_MINI_OUTPUT_PER_1M = 0.60

_SYSTEM = """You help find Instagram Reels that match a business's niche.
Given website copy, output JSON only (no markdown) with:
{
  "tags": ["short topical tags", ...],  // 5-12 tags
  "queries": ["instagram keyword search phrases", ...]  // 3-N phrases
}
Rules for queries:
- Phrases people would type into Instagram keyword/explore search
- Prefer concrete niches, audiences, formats (e.g. "romance novels india", "indie author tips")
- Mix broad + specific; avoid brand-only vanity terms unless distinctive
- English unless the site is clearly another language
- No hashtags (#), no @mentions, no URLs
"""


def _openai_client() -> OpenAI:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for discover query generation")
    return OpenAI(api_key=key)


def _website_blob(website: dict[str, Any]) -> str:
    parts = [
        f"URL: {website.get('final_url') or website.get('url')}",
        f"Title: {website.get('title') or ''}",
        f"Description: {website.get('description') or ''}",
        "Headings:",
        "\n".join(f"- {h}" for h in (website.get("headings") or [])[:15]),
        "Body:",
        (website.get("text") or "")[:8000],
    ]
    return "\n".join(parts)


def _parse_json_payload(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def generate_tags_and_queries(website: dict[str, Any]) -> dict[str, Any]:
    """Return tags, queries, and LLM usage/cost."""
    max_q = DISCOVER_MAX_QUERIES
    model = DISCOVER_LLM_MODEL
    client = _openai_client()
    user = (
        f"Produce at most {max_q} search queries and useful tags for this site:\n\n"
        + _website_blob(website)
    )
    log.info("generating queries model=%s max_queries=%s", model, max_q)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.4,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
    )
    choice = resp.choices[0].message.content or "{}"
    data = _parse_json_payload(choice)
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
    queries: list[str] = []
    for q in data.get("queries") or []:
        s = re.sub(r"[#@]+", "", str(q)).strip()
        s = re.sub(r"\s+", " ", s)
        if s and s.lower() not in {x.lower() for x in queries}:
            queries.append(s)
        if len(queries) >= max_q:
            break

    usage = resp.usage
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = cost_from_tokens(
        in_tok, out_tok, GPT4O_MINI_INPUT_PER_1M, GPT4O_MINI_OUTPUT_PER_1M
    )
    return {
        "tags": tags[:12],
        "queries": queries,
        "usage": {
            "model": model,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "input_rate_usd_per_1m": GPT4O_MINI_INPUT_PER_1M,
            "output_rate_usd_per_1m": GPT4O_MINI_OUTPUT_PER_1M,
            "cost_usd": cost,
        },
        "cost_usd": cost,
    }
