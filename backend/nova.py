"""Amazon Bedrock Nova 2 Lite reel-hook analysis (modular, optional).

Sends a first-12s / 480p-short-side clip plus transcript and deterministic
cut timestamps. Does not call AWS until credentials are present in .env.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Union

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

load_dotenv(Path(__file__).resolve().parent / ".env")

try:
    from usage_costs import nova_usage_block
except ImportError:  # pragma: no cover
    nova_usage_block = None  # type: ignore

NOVA_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
NOVA_CLIP_SEC = 12.0
NOVA_MAX_SHORT_SIDE = 480
NOVA_READ_TIMEOUT_SEC = 300

_FORMAT_BY_SUFFIX = {
    ".mp4": "mp4",
    ".mov": "mov",
    ".mkv": "mkv",
    ".webm": "webm",
    ".flv": "flv",
    ".mpeg": "mpeg",
    ".mpg": "mpg",
    ".wmv": "wmv",
    ".3gp": "three_gp",
}


class NovaCredentialsError(Exception):
    """AWS credentials missing or invalid."""


class NovaVideoError(Exception):
    """Video file missing, unreadable, or clip prep failed."""


class NovaAPIError(Exception):
    """Bedrock / Nova API failure."""


class NovaResponseError(Exception):
    """Model returned non-JSON or schema-invalid output."""


class NovaHookResult(BaseModel):
    deliberate_hook_exists: bool
    hook_strength: int = Field(ge=0, le=100)
    hook_trigger_timestamp: Optional[float] = None
    hook_window_start: Optional[float] = None
    hook_window_end: Optional[float] = None
    hook_resolution_timestamp: Optional[float] = None
    hook_type: Optional[str] = None
    verbal_mechanism: Optional[str] = None
    visual_mechanism: Optional[str] = None
    pattern_interrupt: Optional[str] = None
    curiosity_gap: Optional[str] = None
    retention_explanation: str


NOVA_SYSTEM_PROMPT = """
You are a strict short-form reel hook analyst.

Analyze ONLY the opening 4–12 seconds of the attached video (a pre-trimmed clip).
Judge the opening the way a scrolling viewer would — before any later payoff.

DEFINITIONS (do not conflate these):
- Hook trigger: the precise moment/event that FIRST captures attention.
- Hook window: the continuous interval during which the hook unfolds after the trigger.
- Hook resolution: when the immediate attention-grabbing event ends (open loop may still remain).

A deliberate hook is a pattern interrupt and/or curiosity gap that forces a scroller to stop.
Ordinary talking-head intros, cooking prep, walking B-roll, greetings, or aesthetic footage
without an open loop are NOT hooks.

IMPORTANT CONSTRAINTS:
1. You receive DETECTED SCENE-CUT TIMESTAMPS from a deterministic detector.
   When the hook involves a scene change / smash cut, USE those timestamps.
   Do NOT invent or independently estimate exact cut times.
2. The video track has NO usable audio for you. Spoken content is provided only via
   the SPOKEN TRANSCRIPT field — treat that as ground truth for verbal hooks.
3. Return ONLY a single JSON object. No markdown fences, no commentary.

JSON schema (all keys required; use null for inapplicable optional fields):
{
  "deliberate_hook_exists": boolean,
  "hook_strength": integer 0-100,
  "hook_trigger_timestamp": number|null,
  "hook_window_start": number|null,
  "hook_window_end": number|null,
  "hook_resolution_timestamp": number|null,
  "hook_type": string|null,
  "verbal_mechanism": string|null,
  "visual_mechanism": string|null,
  "pattern_interrupt": string|null,
  "curiosity_gap": string|null,
  "retention_explanation": string
}

If deliberate_hook_exists is false: set timestamps and mechanism fields to null,
set hook_strength to 0, and explain what is missing in retention_explanation.
""".strip()


def _require_credentials() -> None:
    access = (os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    secret = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    if not access or not secret:
        raise NovaCredentialsError(
            "AWS credentials are missing. Set AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY in backend/.env before calling Nova."
        )


def _bedrock_client():
    import boto3
    from botocore.config import Config

    _require_credentials()
    region = (os.getenv("AWS_DEFAULT_REGION") or "us-east-1").strip()
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(read_timeout=NOVA_READ_TIMEOUT_SEC, retries={"max_attempts": 2}),
    )


def _video_format(path: Path) -> str:
    return _FORMAT_BY_SUFFIX.get(path.suffix.lower(), "mp4")


def _prepare_nova_clip(video_path: Path, out_path: Path) -> Path:
    """Trim first NOVA_CLIP_SEC and scale so short side is NOVA_MAX_SHORT_SIDE."""
    # Scale short side to 480, keep AR, force even dimensions for H.264.
    scale = (
        f"scale='if(lt(iw,ih),{NOVA_MAX_SHORT_SIDE},-2)':"
        f"'if(lt(iw,ih),-2,{NOVA_MAX_SHORT_SIDE})'"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "0",
        "-i",
        str(video_path),
        "-t",
        str(NOVA_CLIP_SEC),
        "-vf",
        scale,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise NovaVideoError("ffmpeg is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-500:]
        raise NovaVideoError(f"Failed to prepare Nova clip: {detail}") from exc

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise NovaVideoError("Nova clip preparation produced an empty file.")
    return out_path


def _build_user_text(transcript: str, scene_cut_timestamps: List[float]) -> str:
    in_window = sorted(
        t for t in scene_cut_timestamps if 0 < float(t) <= NOVA_CLIP_SEC + 0.01
    )
    if in_window:
        cut_line = ", ".join(f"{t:.3f}s" for t in in_window)
    else:
        cut_line = "None in the first 12 seconds."

    return (
        f"SPOKEN TRANSCRIPT (audio extracted separately; video has no audio for you):\n"
        f"\"{transcript}\"\n\n"
        f"DETECTED SCENE-CUT TIMESTAMPS (deterministic; use for cut-based hooks):\n"
        f"{cut_line}\n\n"
        "Analyze the attached opening clip (first ~12 seconds at reduced resolution). "
        "Return JSON only."
    )


def _extract_assistant_text(response: dict) -> str:
    try:
        content = response["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise NovaResponseError(f"Unexpected Bedrock response shape: {exc}") from exc

    parts: List[str] = []
    for block in content or []:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    text = "\n".join(parts).strip()
    if not text:
        raise NovaResponseError("Nova returned an empty text response.")
    return text


def _parse_json_response(text: str) -> dict:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                raise NovaResponseError(
                    f"Malformed Nova JSON response: {exc}"
                ) from exc
        raise NovaResponseError("Nova response did not contain valid JSON.")


def analyze_hook_with_nova(
    video_path: Union[str, Path],
    transcript: str,
    scene_cut_timestamps: List[float],
) -> dict:
    """Call Nova 2 Lite for structured hook analysis of a reel opening.

    Raises NovaCredentialsError without contacting AWS when keys are empty.
    """
    path = Path(video_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    _require_credentials()

    tmp_dir = None
    clip_path: Optional[Path] = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="nova_hook_")
        clip_path = Path(tmp_dir) / f"{path.stem}_nova12s.mp4"
        _prepare_nova_clip(path, clip_path)
        video_bytes = clip_path.read_bytes()
        if not video_bytes:
            raise NovaVideoError(f"Could not read Nova clip bytes: {clip_path}")

        client = _bedrock_client()
        user_text = _build_user_text(transcript or "", list(scene_cut_timestamps or []))

        try:
            response = client.converse(
                modelId=NOVA_MODEL_ID,
                system=[{"text": NOVA_SYSTEM_PROMPT}],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "video": {
                                    "format": _video_format(clip_path),
                                    "source": {"bytes": video_bytes},
                                }
                            },
                            {"text": user_text},
                        ],
                    }
                ],
                inferenceConfig={"temperature": 0.0, "maxTokens": 2048},
            )
        except Exception as exc:
            # Lazy import so missing boto3 still allows credential-skip path above.
            try:
                from botocore.exceptions import BotoCoreError, ClientError
            except ImportError:
                raise NovaAPIError(f"Bedrock call failed: {exc}") from exc

            if isinstance(exc, ClientError):
                code = (
                    (exc.response or {}).get("Error", {}).get("Code", "")
                    or ""
                )
                auth_codes = {
                    "UnrecognizedClientException",
                    "InvalidSignatureException",
                    "AccessDeniedException",
                    "IncompleteSignature",
                    "AuthFailure",
                }
                if code in auth_codes:
                    raise NovaCredentialsError(
                        f"AWS credentials rejected by Bedrock ({code}): {exc}"
                    ) from exc
                raise NovaAPIError(f"Bedrock API error ({code or 'unknown'}): {exc}") from exc
            if isinstance(exc, BotoCoreError):
                raise NovaAPIError(f"Bedrock client error: {exc}") from exc
            raise NovaAPIError(f"Bedrock call failed: {exc}") from exc

        raw_text = _extract_assistant_text(response)
        raw_obj = _parse_json_response(raw_text)
        try:
            parsed = NovaHookResult.model_validate(raw_obj)
        except ValidationError as exc:
            raise NovaResponseError(f"Nova JSON failed schema validation: {exc}") from exc

        out = parsed.model_dump()
        out["model_id"] = NOVA_MODEL_ID
        out["clip_seconds"] = NOVA_CLIP_SEC
        out["max_short_side"] = NOVA_MAX_SHORT_SIDE

        usage = response.get("usage") or {}
        input_tokens = int(usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("outputTokens") or 0)
        total_tokens = int(usage.get("totalTokens") or (input_tokens + output_tokens))
        if nova_usage_block is not None:
            out["usage"] = nova_usage_block(
                model=NOVA_MODEL_ID,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        else:
            out["usage"] = {
                "model": NOVA_MODEL_ID,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }
        return out
    finally:
        if clip_path is not None and clip_path.exists():
            try:
                clip_path.unlink()
            except OSError:
                pass
        if tmp_dir is not None:
            try:
                Path(tmp_dir).rmdir()
            except OSError:
                pass
