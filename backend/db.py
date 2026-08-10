"""Supabase client helpers for reel upserts."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("reel-api")

_client = None
_client_error: Optional[str] = None


def _get_client():
    global _client, _client_error
    if _client is not None:
        return _client
    if _client_error is not None:
        raise RuntimeError(_client_error)

    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        _client_error = (
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in backend/.env"
        )
        raise RuntimeError(_client_error)

    try:
        from supabase import create_client
    except ImportError as exc:
        _client_error = "supabase package not installed — pip install -r requirements.txt"
        raise RuntimeError(_client_error) from exc

    _client = create_client(url, key)
    return _client


def upsert_reels(rows: List[Dict[str, Any]]) -> int:
    """Upsert reel metadata by id. Returns number of rows sent.

    Only updates username/music/likes/comments/reposts/updated_at so
    breakthrough/score/deeper_insights/created_at stay intact.
    """
    if not rows:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for row in rows:
        payload.append(
            {
                "id": row["id"],
                "username": row["username"],
                "music": row.get("music"),
                "likes": row["likes"],
                "comments": row["comments"],
                "reposts": row["reposts"],
                "updated_at": now,
            }
        )

    client = _get_client()
    # Prefer upsert with explicit update columns when the client supports it.
    try:
        (
            client.table("reels")
            .upsert(
                payload,
                on_conflict="id",
            )
            .execute()
        )
    except TypeError:
        client.table("reels").upsert(payload).execute()

    return len(payload)
