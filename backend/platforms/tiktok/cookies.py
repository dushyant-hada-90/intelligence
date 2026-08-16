"""TikTok cookie jar from Netscape file and/or .env."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from config.env import BACKEND_DIR

log = logging.getLogger("tt-cookies")


def strip_cookie_value(raw: str) -> str:
    val = (raw or "").strip().strip('"').strip("'")
    if "=" in val and not val.startswith("http"):
        name, _, rest = val.partition("=")
        if name.strip().lower() in {
            "sid_tt",
            "sessionid",
            "mstoken",
            "ttwid",
            "tt_csrf_token",
        }:
            return rest.strip()
    return val


def parse_netscape_cookie_file(path: Path) -> dict[str, str]:
    """Parse Netscape cookies.txt; later rows win for duplicate names."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5], parts[6]
        if name:
            out[name] = value
    return out


def env_cookies() -> dict[str, str]:
    """Build cookie jar from Netscape file and/or .env (logged-in TikTok web session)."""
    out: dict[str, str] = {}

    file_env = (os.getenv("TIKTOK_COOKIES_FILE") or "").strip()
    candidates = []
    if file_env:
        p = Path(file_env)
        if not p.is_absolute():
            p = BACKEND_DIR / p
        candidates.append(p)
    candidates.append(BACKEND_DIR / "tiktok_cookies.txt")
    for path in candidates:
        parsed = parse_netscape_cookie_file(path)
        if parsed:
            out.update(parsed)
            log.info("loaded %s TikTok cookies from %s", len(parsed), path.name)
            break

    blob = (os.getenv("TIKTOK_COOKIES") or "").strip()
    if blob:
        for part in blob.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            value = value.strip()
            if name and value:
                out[name] = value

    sid = strip_cookie_value(
        os.getenv("TIKTOK_SESSIONID") or os.getenv("TIKTOK_SID_TT") or ""
    )
    if sid:
        out["sid_tt"] = sid
        out.setdefault("sessionid", sid)

    ms = strip_cookie_value(os.getenv("TIKTOK_MS_TOKEN") or "")
    if ms:
        out["msToken"] = ms

    ttwid = strip_cookie_value(os.getenv("TIKTOK_TTWID") or "")
    if ttwid:
        out["ttwid"] = ttwid

    csrf = strip_cookie_value(
        os.getenv("TIKTOK_CSRF_TOKEN") or os.getenv("TIKTOK_TT_CSRF_TOKEN") or ""
    )
    if csrf:
        out["tt_csrf_token"] = csrf

    if "sid_tt" in out or "sessionid" in out:
        out.pop("guest_mode_flag", None)
    return out


def require_session_cookies() -> dict[str, str]:
    cookies = env_cookies()
    if not (cookies.get("sid_tt") or cookies.get("sessionid")):
        raise RuntimeError(
            "TikTok search requires logged-in cookies in backend/.env.\n"
            "Set TIKTOK_SESSIONID (sid_tt value from DevTools → Application → Cookies).\n"
            "Also set TIKTOK_MS_TOKEN and TIKTOK_TTWID when possible.\n"
            "Optional: TIKTOK_COOKIES='sid_tt=...; msToken=...; ttwid=...'"
        )
    return cookies


def cookie_header(cookies: dict[str, str]) -> str:
    """Single Cookie header (avoids httpx jar 'Multiple cookies exist with name=…')."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v is not None)
