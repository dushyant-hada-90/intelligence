"""FastAPI reel decision server — watch duration + engage actions per batch."""

from __future__ import annotations

import logging
import random
from typing import List, Literal, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=SERVER_PORT,
        reload=False,
        log_level="info",
    )
