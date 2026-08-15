"""Nova-first Instagram reel hook analyzer (library + CLI).

Pipeline: download → ffmpeg audio → Whisper → scene cuts → Nova 2 Lite.
No OpenAI frame/vision stack. Gradio removed — use the FastAPI backend.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from nova import analyze_hook_with_nova
from usage_costs import build_usage_report, whisper_usage_block

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")

DOWNLOADS_DIR = BACKEND_DIR / "downloads"
COOKIES_FILE = BACKEND_DIR / "cookies.txt"
COOKIE_BROWSERS = ("brave", "firefox", "edge")
MAX_HOOK_WINDOW_SEC = 5.0
CUT_DETECTOR_THRESHOLD = 27.0
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
ProgressCb = Optional[Callable[[float, str], None]]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def _note(progress: ProgressCb, fraction: float, message: str) -> None:
    print(message)
    if progress:
        progress(fraction, message)


def extract_audio_ffmpeg(video_path: str, output_audio_path: str) -> bool:
    """Extract audio from the first hook window."""
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "00:00:00",
        "-i",
        video_path,
        "-t",
        str(MAX_HOOK_WINDOW_SEC),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-ar",
        "16000",
        "-ac",
        "1",
        output_audio_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception as e:
        print(f"[FFmpeg Warning] Could not extract audio: {e}")
        return False


def detect_scenes(video_path: str) -> List[dict]:
    """Return shot ranges [{start, end}, ...] using PySceneDetect."""
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector

    video = open_video(str(video_path))
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=CUT_DETECTOR_THRESHOLD))
    scene_manager.detect_scenes(video)
    scenes = []
    for start, end in scene_manager.get_scene_list():
        scenes.append(
            {
                "start": round(start.seconds, 3),
                "end": round(end.seconds, 3),
            }
        )
    return scenes


def cut_times_from_scenes(scenes: List[dict]) -> List[float]:
    return [scene["start"] for scene in scenes[1:] if scene.get("start", 0) > 0.04]


def _audio_duration_seconds(audio_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nokey=1:noprint_wrappers=1",
        audio_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        value = float((result.stdout or "").strip())
        if value > 0:
            return value
    except Exception:
        pass
    return float(MAX_HOOK_WINDOW_SEC)


def transcribe_audio(client: OpenAI, audio_path: str) -> tuple[str, dict]:
    empty_usage = whisper_usage_block(model="whisper-1", audio_seconds=0.0)
    if not Path(audio_path).exists() or os.path.getsize(audio_path) == 0:
        return "No audio available or video is silent.", empty_usage

    audio_seconds = _audio_duration_seconds(audio_path)
    usage = whisper_usage_block(model="whisper-1", audio_seconds=audio_seconds)
    try:
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
            )
        text = transcript.strip() if transcript else "No speech detected."
        return text, usage
    except Exception as e:
        usage["error"] = str(e)
        return f"Audio transcription failed: {e}", usage


def _ydl_download_opts(**extra) -> dict:
    opts = {
        "format": "bv*[vcodec^=avc]+ba/b[vcodec^=avc]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "overwrites": True,
        "restrictfilenames": True,
    }
    opts.update(extra)
    return opts


def _extract_downloaded_path(ydl, info) -> Path:
    if info is None:
        raise RuntimeError("yt-dlp returned no video info.")
    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    path = Path(ydl.prepare_filename(info))
    if not path.exists():
        merged = path.with_suffix(".mp4")
        if merged.exists():
            path = merged
    if not path.exists():
        raise RuntimeError(f"Download finished but the video file was not found: {path}")
    return path


def _write_env_cookie_file() -> Optional[Path]:
    session_id = (os.getenv("INSTAGRAM_SESSIONID") or "").strip().strip('"').strip("'")
    if not session_id:
        return None
    if session_id.lower().startswith("sessionid="):
        session_id = session_id.split("=", 1)[1].strip()

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    cookie_path = DOWNLOADS_DIR / "instagram_env_cookies.txt"
    lines = [
        "# Netscape HTTP Cookie File",
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t" + session_id,
    ]
    ds_user_id = (os.getenv("INSTAGRAM_DS_USER_ID") or "").strip()
    csrf = (os.getenv("INSTAGRAM_CSRFTOKEN") or "").strip()
    if ds_user_id:
        lines.append(".instagram.com\tTRUE\t/\tTRUE\t2147483647\tds_user_id\t" + ds_user_id)
    if csrf:
        lines.append(".instagram.com\tTRUE\t/\tTRUE\t2147483647\tcsrftoken\t" + csrf)
    cookie_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cookie_path


def download_reel(url: str, progress: ProgressCb = None) -> Path:
    """Download an Instagram reel."""
    import yt_dlp

    cleaned = url.strip()
    if not INSTAGRAM_URL_RE.search(cleaned):
        raise ValueError(
            "Paste a full Instagram Reel URL, e.g. https://www.instagram.com/reel/XXXX/"
        )

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    attempts: list[tuple[str, dict]] = []
    env_cookies = _write_env_cookie_file()
    if env_cookies:
        attempts.append(
            ("INSTAGRAM_SESSIONID from .env", _ydl_download_opts(cookiefile=str(env_cookies)))
        )
    if COOKIES_FILE.exists():
        attempts.append(
            (f"cookie file {COOKIES_FILE.name}", _ydl_download_opts(cookiefile=str(COOKIES_FILE)))
        )
    for browser in COOKIE_BROWSERS:
        attempts.append(
            (f"{browser} cookies", _ydl_download_opts(cookiesfrombrowser=(browser,)))
        )

    last_error = None
    for label, opts in attempts:
        _note(progress, 0.05, f"Downloading reel with {label}...")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(cleaned, download=True)
                path = _extract_downloaded_path(ydl, info)
            _note(progress, 0.35, f"Downloaded {path.name} ({label})")
            return path
        except Exception as exc:
            last_error = str(exc)
            print(f"{label} failed: {last_error}")
            continue

    raise RuntimeError(
        "Could not download the reel.\n\n"
        "Workarounds:\n"
        "1. Stay logged into Instagram in Brave or Firefox and retry.\n"
        "2. Paste INSTAGRAM_SESSIONID into .env (this folder).\n"
        f"3. Or save a Netscape cookies.txt as:\n   {COOKIES_FILE}\n"
        f"Last error: {last_error}"
    )


def analyze_video(
    video_path: Path,
    progress: ProgressCb = None,
    source_url: Optional[str] = None,
    timings: Optional[dict] = None,
) -> dict:
    """Run Nova-first hook analysis. Raises on Nova/AWS failure."""
    client = OpenAI()
    video_path = Path(video_path)
    timings = timings if timings is not None else {}
    pipeline_started = time.perf_counter()

    temp_audio_path = DOWNLOADS_DIR / f"{video_path.stem}_audio.mp3"
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    _note(progress, 0.40, "Extracting audio with FFmpeg...")
    t0 = time.perf_counter()
    has_audio = extract_audio_ffmpeg(str(video_path), str(temp_audio_path))
    timings["extract_audio_ms"] = _elapsed_ms(t0)

    _note(progress, 0.55, "Transcribing speech...")
    t0 = time.perf_counter()
    if has_audio:
        transcript, whisper_usage = transcribe_audio(client, str(temp_audio_path))
    else:
        transcript = "No audio."
        whisper_usage = whisper_usage_block(model="whisper-1", audio_seconds=0.0)
    timings["transcribe_ms"] = _elapsed_ms(t0)
    if temp_audio_path.exists():
        os.remove(temp_audio_path)

    _note(progress, 0.62, "Detecting shot cuts...")
    t0 = time.perf_counter()
    try:
        scenes = detect_scenes(str(video_path))
    except Exception as exc:
        print(f"[PySceneDetect Warning] {exc}")
        scenes = []
    timings["detect_cuts_ms"] = _elapsed_ms(t0)
    cut_times = cut_times_from_scenes(scenes)

    _note(progress, 0.75, "Nova 2 Lite hook analysis...")
    t0 = time.perf_counter()
    nova_hook = analyze_hook_with_nova(video_path, transcript, cut_times)
    timings["nova_inference_ms"] = _elapsed_ms(t0)
    nova_usage = nova_hook.get("usage") if isinstance(nova_hook, dict) else None

    timings["analyze_video_ms"] = _elapsed_ms(pipeline_started)
    usage = build_usage_report(
        whisper=whisper_usage,
        nova=nova_usage if isinstance(nova_usage, dict) else None,
    )

    result = {
        "source_url": source_url,
        "video_path": str(video_path.resolve()),
        "transcript": transcript,
        "scenes": scenes,
        "cut_times": cut_times,
        "nova_hook": nova_hook,
        "deliberate_hook_exists": bool(nova_hook.get("deliberate_hook_exists")),
        "hook_strength": nova_hook.get("hook_strength"),
        "timings": timings,
        "usage": usage,
        "cost_usd": (usage.get("totals") or {}).get("combined_run_usd"),
    }
    _note(progress, 1.0, "Done.")
    return result


def analyze_reel_url(url: str, progress: ProgressCb = None) -> tuple[dict, Path]:
    timings: dict = {}
    t0 = time.perf_counter()
    video_path = download_reel(url, progress)
    timings["download_ms"] = _elapsed_ms(t0)
    result = analyze_video(
        video_path, progress, source_url=url.strip(), timings=timings
    )
    return result, video_path


def main() -> None:
    if len(sys.argv) <= 1:
        print("Usage: python hook_pipeline.py <instagram_reel_url|local_video_path>")
        print("API: uvicorn app:app --host 127.0.0.1 --port 7860")
        sys.exit(1)

    source = sys.argv[1]
    if source.startswith("http://") or source.startswith("https://"):
        result, video_path = analyze_reel_url(source)
    else:
        video_path = Path(source)
        if not video_path.exists():
            print(f"Video not found: {source}")
            sys.exit(1)
        print(f"Analyzing: {video_path}")
        result = analyze_video(video_path)

    print("\n" + "=" * 60)
    print("NOVA HOOK ANALYSIS")
    print("=" * 60)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nVideo: {video_path.resolve()}")
    print(f"Cost USD: {result.get('cost_usd')}")


if __name__ == "__main__":
    main()
