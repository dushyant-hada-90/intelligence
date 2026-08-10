"""FastAPI reel decision + ingest server."""

from __future__ import annotations

import logging
import random
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from db import upsert_reels

# Keep in sync with reel-timing-extension content-script.js API_BASE_URL
SERVER_PORT = 7860

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reel-api")

app = FastAPI(title="Reel Decision API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.instagram.com",
        "http://localhost",
        "http://127.0.0.1",
        "*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def private_network_access(request, call_next):
    """Chrome Private Network Access preflight from public HTTPS → localhost."""
    if request.method == "OPTIONS":
        from starlette.responses import Response

        headers = {
            "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": request.headers.get(
                "access-control-request-headers", "*"
            ),
            "Access-Control-Allow-Private-Network": "true",
        }
        return Response(status_code=204, headers=headers)
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


class ReelMetadata(BaseModel):
    id: str


class ReelResponse(BaseModel):
    id: str
    action: Optional[Literal["like", "save"]] = Field(
        default=None, description='One of "like", "save", or null'
    )
    comment: Optional[str] = None
    duration: float


class ReelIngestItem(BaseModel):
    id: str
    username: str = "unknown"
    music: Optional[str] = None
    # null = like count hidden/unknown (IG like_and_view_counts_disabled)
    likes: Optional[int] = None
    comments: int = 0
    reposts: int = 0


COMMENT_POOL = ["Great reel!", "Nice one.", "hii", "Fire 🔥"]


def decide_for_reel(reel_id: str) -> ReelResponse:
    """Stub decision policy — replace with real model later.

    `duration` is **watch time only** (seconds the user should view the reel
    before like/save/comment). Engage time is separate and happens after.
    """
    action = random.choice(["like", "save", None])
    # 50% chance of a comment (independent of like/save).
    comment = random.choice(COMMENT_POOL) if random.random() < 0.5 else None
    duration = round(random.uniform(3, 12), 2)
    # Humans watch the reel before typing — require a real dwell when commenting.
    if comment is not None:
        duration = max(duration, 15.0)
    return ReelResponse(
        id=reel_id, action=action, comment=comment, duration=duration
    )


def normalize_ingest_rows(reels: List[ReelIngestItem]) -> List[dict]:
    """Trim ids, default username, clamp counts, dedupe by id (last wins)."""
    by_id: dict[str, dict] = {}
    for reel in reels:
        rid = (reel.id or "").strip()
        if not rid:
            continue
        username = (reel.username or "").strip() or "unknown"
        music = reel.music.strip() if isinstance(reel.music, str) and reel.music.strip() else None
        likes = None if reel.likes is None else max(0, int(reel.likes))
        by_id[rid] = {
            "id": rid,
            "username": username,
            "music": music,
            "likes": likes,
            "comments": max(0, int(reel.comments or 0)),
            "reposts": max(0, int(reel.reposts or 0)),
        }
    return list(by_id.values())


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/reels", response_model=List[ReelResponse])
def process_reels(reels: List[ReelMetadata]) -> List[ReelResponse]:
    responses = [decide_for_reel(reel.id) for reel in reels]
    for r in responses:
        log.info(
            "decision id=%s action=%s comment=%s duration=%s",
            r.id,
            r.action,
            r.comment,
            r.duration,
        )
    return responses


@app.post("/reels/ingest")
def ingest_reels(reels: List[ReelIngestItem]) -> dict:
    rows = normalize_ingest_rows(reels)
    if not rows:
        return {"upserted": 0}
    try:
        n = upsert_reels(rows)
    except Exception as exc:
        log.exception("ingest upsert failed count=%s", len(rows))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log.info("ingest upserted=%s", n)
    return {"upserted": n}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=SERVER_PORT,
        reload=False,
        log_level="info",
    )
