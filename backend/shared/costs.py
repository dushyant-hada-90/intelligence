"""Token / transcription cost helpers for the reel hook analyzer.

Rates are public list prices (USD). Verify against vendor pages before billing.
Last checked: 2026-08-15.

Sources:
- whisper-1: OpenAI API pricing — $0.006 / minute of audio
- Nova 2 Lite (Bedrock global cross-region Standard): ~$0.30 / 1M input, $2.50 / 1M output
"""

from __future__ import annotations

from typing import Any, Dict, Optional

NOVA2_LITE_INPUT_PER_1M = 0.30
NOVA2_LITE_OUTPUT_PER_1M = 2.50
WHISPER_1_PER_MINUTE = 0.006

PRICING_NOTES = (
    "Estimates from public list prices (Aug 2026). "
    "whisper-1 $0.006/min; "
    "Nova 2 Lite global Standard ~$0.30/$2.50 per 1M in/out. "
    "Bedrock geo/in-region and tiers may differ."
)


def _usd(amount: float) -> float:
    return round(float(amount), 8)


def cost_from_tokens(
    input_tokens: int,
    output_tokens: int,
    input_per_1m: float,
    output_per_1m: float,
) -> float:
    return _usd(
        (max(0, input_tokens) / 1_000_000.0) * input_per_1m
        + (max(0, output_tokens) / 1_000_000.0) * output_per_1m
    )


def whisper_cost_usd(audio_seconds: float) -> float:
    minutes = max(0.0, float(audio_seconds)) / 60.0
    return _usd(minutes * WHISPER_1_PER_MINUTE)


def nova_usage_block(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    cost = cost_from_tokens(
        input_tokens,
        output_tokens,
        NOVA2_LITE_INPUT_PER_1M,
        NOVA2_LITE_OUTPUT_PER_1M,
    )
    total = total_tokens if total_tokens is not None else input_tokens + output_tokens
    return {
        "model": model,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total),
        "input_rate_usd_per_1m": NOVA2_LITE_INPUT_PER_1M,
        "output_rate_usd_per_1m": NOVA2_LITE_OUTPUT_PER_1M,
        "cost_usd": cost,
    }


def whisper_usage_block(*, model: str, audio_seconds: float) -> Dict[str, Any]:
    return {
        "model": model,
        "audio_seconds": round(float(audio_seconds), 3),
        "rate_usd_per_minute": WHISPER_1_PER_MINUTE,
        "cost_usd": whisper_cost_usd(audio_seconds),
    }


def build_usage_report(
    *,
    whisper: Optional[Dict[str, Any]] = None,
    nova: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    whisper_cost = float((whisper or {}).get("cost_usd") or 0.0)
    nova_cost = float((nova or {}).get("cost_usd") or 0.0)
    combined = _usd(whisper_cost + nova_cost)

    return {
        "pricing_notes": PRICING_NOTES,
        "whisper": whisper,
        "nova": nova,
        "totals": {
            "combined_run_usd": combined,
            "whisper_usd": _usd(whisper_cost),
            "nova_usd": _usd(nova_cost),
        },
    }
