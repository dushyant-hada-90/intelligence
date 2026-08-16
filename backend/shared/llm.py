"""OpenAI helpers for discover query generation."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI

GPT4O_MINI_INPUT_PER_1M = 0.15
GPT4O_MINI_OUTPUT_PER_1M = 0.60


def openai_client(*, purpose: str = "LLM") -> OpenAI:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(f"OPENAI_API_KEY is required for {purpose}")
    return OpenAI(api_key=key)


def parse_json_payload(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)
