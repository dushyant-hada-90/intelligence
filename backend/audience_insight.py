"""LLM: whoWatched / whyWatched audience insight for hook analysis."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from openai import OpenAI

from tunables import DISCOVER_LLM_MODEL
from usage_costs import cost_from_tokens

log = logging.getLogger("audience-insight")

GPT4O_MINI_INPUT_PER_1M = 0.15
GPT4O_MINI_OUTPUT_PER_1M = 0.60

_SYSTEM = """You analyze short-form reels for audience fit and watch reasons.
Return JSON only (no markdown) with exactly:
{
  "whoWatched": "string of clear bullet points",
  "whyWatched": "string of clear bullet points"
}

whoWatched:
- Describe the types of people likely to stop and watch (audience segments).
- Examples of segment style: doctors, couples, tech enthusiasts, fitness beginners, finance grads.
- 2–5 bullets. Each bullet is one audience type + a short qualifier if useful.
- Do NOT invent demographics you cannot support from the content.

whyWatched:
- Explain what makes this reel worth watching / what works (hook, curiosity gap, visual punch, relatability, payoff tease, etc.).
- 2–5 bullets. Concrete and specific to THIS reel, not generic advice.
- If the opening is weak, still say honestly what might hold a niche viewer — or what fails.

Format both strings as plain text with one bullet per line, each line starting with "- ".
No hashtags, no URLs, no markdown bold/italic.
"""


def _openai_client() -> OpenAI:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for whoWatched / whyWatched")
    return OpenAI(api_key=key)


def _parse_json_payload(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _as_bullet_string(value: Any) -> str:
    """Normalize list or string into newline-separated '- ' bullets."""
    if value is None:
        return ""
    if isinstance(value, list):
        lines = [str(x).strip() for x in value if str(x).strip()]
    else:
        text = str(value).replace("\r\n", "\n").strip()
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out: list[str] = []
    for ln in lines:
        ln = re.sub(r"^[-•*]\s*", "", ln).strip()
        if not ln:
            continue
        out.append(f"- {ln}")
    return "\n".join(out)


def generate_who_why_watched(
    *,
    transcript: str,
    nova_hook: Optional[dict[str, Any]] = None,
    source_url: Optional[str] = None,
) -> dict[str, Any]:
    """Return whoWatched, whyWatched, usage, cost_usd."""
    nova = nova_hook or {}
    # Drop heavy nested usage from prompt context
    nova_slim = {
        k: v
        for k, v in nova.items()
        if k
        in {
            "deliberate_hook_exists",
            "hook_strength",
            "hook_type",
            "verbal_mechanism",
            "visual_mechanism",
            "pattern_interrupt",
            "curiosity_gap",
            "retention_explanation",
            "hook_trigger_timestamp",
            "hook_window_start",
            "hook_window_end",
        }
    }
    model = (os.getenv("AUDIENCE_LLM_MODEL") or DISCOVER_LLM_MODEL or "gpt-4o-mini").strip()
    client = _openai_client()
    user = (
        f"Source URL: {source_url or 'unknown'}\n\n"
        f"SPOKEN TRANSCRIPT:\n{(transcript or '').strip() or '(none)'}\n\n"
        f"HOOK ANALYSIS JSON:\n{json.dumps(nova_slim, ensure_ascii=False)}\n"
    )
    log.info("generating whoWatched/whyWatched model=%s", model)
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
    who = _as_bullet_string(data.get("whoWatched"))
    why = _as_bullet_string(data.get("whyWatched"))
    if not who:
        who = "- General short-form scrollers (audience unclear from available signals)"
    if not why:
        why = "- Insufficient signal to name a clear watch reason"

    usage = resp.usage
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = cost_from_tokens(
        in_tok, out_tok, GPT4O_MINI_INPUT_PER_1M, GPT4O_MINI_OUTPUT_PER_1M
    )
    return {
        "whoWatched": who,
        "whyWatched": why,
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
