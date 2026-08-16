"""yt-dlp downloaders for Instagram and TikTok reels."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

from config.env import BACKEND_DIR
from platforms import (
    INSTAGRAM_URL_RE,
    TIKTOK_URL_RE,
    canonicalize_video_url,
    detect_platform,
)

DOWNLOADS_DIR = BACKEND_DIR / "downloads"
COOKIES_FILE = BACKEND_DIR / "cookies.txt"
COOKIE_BROWSERS = ("brave", "firefox", "edge")
ProgressCb = Optional[Callable[[float, str], None]]


def _note(progress: ProgressCb, fraction: float, message: str) -> None:
    print(message)
    if progress:
        progress(fraction, message)


def _clean_ydl_error(exc: BaseException | str) -> str:
    """Strip yt-dlp ANSI colors and collapse whitespace for API/UI errors."""
    text = str(exc)
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = text.replace("ERROR:", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:500]


def _classify_download_failure(message: str) -> str:
    low = message.lower()
    if "login required" in low or "please log in" in low or "login_required" in low:
        return "instagram_login_required"
    if "rate-limit" in low or "rate limit" in low or "429" in low:
        return "rate_limited"
    if "not available" in low or "private" in low or "404" in low:
        return "unavailable"
    if "could not find" in low and "cookies database" in low:
        return "browser_cookies_missing"
    if "cookie" in low and ("expired" in low or "invalid" in low):
        return "cookies_invalid"
    if "unsupported url" in low:
        return "bad_url"
    return "download_failed"


def _format_download_error(
    *,
    platform: str,
    url: str,
    attempts: list[tuple[str, str]],
) -> str:
    """Human-readable multi-attempt download failure for debugging."""
    if not attempts:
        return f"{platform} download failed for {url} (no attempts recorded)."

    classified = [(_classify_download_failure(err), label, err) for label, err in attempts]
    priority = {
        "instagram_login_required": 0,
        "cookies_invalid": 1,
        "rate_limited": 2,
        "unavailable": 3,
        "bad_url": 4,
        "download_failed": 5,
        "browser_cookies_missing": 9,
    }
    classified.sort(key=lambda row: priority.get(row[0], 8))
    kind, best_label, best_err = classified[0]

    lines = [
        f"{platform} download failed.",
        f"URL: {url}",
        f"Likely cause: {kind} (via {best_label})",
        f"Detail: {best_err}",
        "Attempts:",
    ]
    for label, err in attempts:
        lines.append(f"  - {label}: {err}")

    if platform == "Instagram":
        lines.extend(
            [
                "Fix:",
                "  1. Set a fresh INSTAGRAM_SESSIONID in backend/.env (and restart).",
                f"  2. Or export Netscape cookies to {COOKIES_FILE}.",
                "  3. Browser-cookie fallbacks only work if that browser is installed and logged into Instagram.",
            ]
        )
    else:
        lines.extend(
            [
                "Fix:",
                "  1. Retry with a canonical https://www.tiktok.com/@user/video/<id> URL.",
                f"  2. Optional: Netscape cookies at {COOKIES_FILE}.",
            ]
        )
    return "\n".join(lines)


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
        "no_color": True,
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


def download_instagram_reel(url: str, progress: ProgressCb = None) -> Path:
    """Download an Instagram reel (session cookies / browser cookies)."""
    import yt_dlp

    cleaned = canonicalize_video_url(url)
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
    else:
        print("INSTAGRAM_SESSIONID not set — skipping .env cookie attempt")
    if COOKIES_FILE.exists():
        attempts.append(
            (f"cookie file {COOKIES_FILE.name}", _ydl_download_opts(cookiefile=str(COOKIES_FILE)))
        )
    for browser in COOKIE_BROWSERS:
        attempts.append(
            (f"{browser} cookies", _ydl_download_opts(cookiesfrombrowser=(browser,)))
        )

    if not attempts:
        raise RuntimeError(
            "Instagram download failed.\n"
            f"URL: {cleaned}\n"
            "Likely cause: no_credentials\n"
            "Detail: INSTAGRAM_SESSIONID is empty and no cookies.txt / browser cookies configured.\n"
            "Fix: set INSTAGRAM_SESSIONID in backend/.env and restart the API."
        )

    failures: list[tuple[str, str]] = []
    for label, opts in attempts:
        _note(progress, 0.05, f"Downloading reel with {label}...")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(cleaned, download=True)
                path = _extract_downloaded_path(ydl, info)
            _note(progress, 0.35, f"Downloaded {path.name} ({label})")
            return path
        except Exception as exc:
            cleaned_err = _clean_ydl_error(exc)
            failures.append((label, cleaned_err))
            print(f"{label} failed: {cleaned_err}")
            continue

    raise RuntimeError(
        _format_download_error(platform="Instagram", url=cleaned, attempts=failures)
    )


def download_tiktok_video(url: str, progress: ProgressCb = None) -> Path:
    """Download a TikTok video via yt-dlp (guest; cookies optional)."""
    import yt_dlp

    cleaned = canonicalize_video_url(url)
    if not TIKTOK_URL_RE.search(cleaned):
        raise ValueError(
            "Paste a full TikTok URL, e.g. https://www.tiktok.com/@user/video/123"
        )

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    attempts: list[tuple[str, dict]] = [
        ("guest (no cookies)", _ydl_download_opts()),
    ]
    if COOKIES_FILE.exists():
        attempts.append(
            (f"cookie file {COOKIES_FILE.name}", _ydl_download_opts(cookiefile=str(COOKIES_FILE)))
        )
    for browser in COOKIE_BROWSERS:
        attempts.append(
            (f"{browser} cookies", _ydl_download_opts(cookiesfrombrowser=(browser,)))
        )

    failures: list[tuple[str, str]] = []
    for label, opts in attempts:
        _note(progress, 0.05, f"Downloading TikTok with {label}...")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(cleaned, download=True)
                path = _extract_downloaded_path(ydl, info)
            _note(progress, 0.35, f"Downloaded {path.name} ({label})")
            return path
        except Exception as exc:
            cleaned_err = _clean_ydl_error(exc)
            failures.append((label, cleaned_err))
            print(f"{label} failed: {cleaned_err}")
            continue

    raise RuntimeError(
        _format_download_error(platform="TikTok", url=cleaned, attempts=failures)
    )


def download_reel(url: str, progress: ProgressCb = None) -> Path:
    """Download a reel/video for any supported platform."""
    cleaned = (url or "").strip()
    platform = detect_platform(cleaned)
    if platform == "instagram":
        return download_instagram_reel(cleaned, progress)
    if platform == "tiktok":
        return download_tiktok_video(cleaned, progress)
    raise ValueError(
        "Paste an Instagram Reel or TikTok video URL, e.g. "
        "https://www.instagram.com/reel/XXXX/ or "
        "https://www.tiktok.com/@user/video/123"
    )
