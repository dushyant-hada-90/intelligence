"""Production FastAPI — Nova-first Instagram reel hook analyzer."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")

from jobs import manager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hook-api")

DEV_UI = (os.getenv("DEV_UI") or "1").strip().lower() in {"1", "true", "yes", "on"}
API_KEY = (os.getenv("API_KEY") or "").strip()
STATIC_DIR = BACKEND_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "Hook API starting DEV_UI=%s max_workers=%s max_queue=%s",
        DEV_UI,
        manager.max_workers,
        manager.max_queue_size,
    )
    yield
    manager.shutdown()
    log.info("Hook API shut down")


app = FastAPI(
    title="Reel Hook Analyzer API",
    version="2.0.0",
    description="Nova-first Instagram reel hook analysis with Whisper + scene cuts.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="Instagram reel/post URL")


class AnalyzeAccepted(BaseModel):
    job_id: str
    status: str = "queued"


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    if not API_KEY:
        return
    if not x_api_key or x_api_key.strip() != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def require_api_key_header_or_query(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    api_key: Optional[str] = None,
) -> None:
    """Video <source> cannot set headers; allow ?api_key= as well."""
    if not API_KEY:
        return
    provided = (x_api_key or api_key or "").strip()
    if provided != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "stack": "nova",
        "dev_ui": DEV_UI,
        "max_concurrent_jobs": manager.max_workers,
        "max_queue_size": manager.max_queue_size,
    }


@app.post("/v1/hooks/analyze", response_model=AnalyzeAccepted)
def analyze_hook(body: AnalyzeRequest, _: None = Depends(require_api_key)) -> AnalyzeAccepted:
    try:
        job = manager.submit(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AnalyzeAccepted(job_id=job.job_id, status=job.status)


@app.get("/v1/hooks/jobs/{job_id}")
def get_job(job_id: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/v1/hooks/jobs/{job_id}/video")
def get_job_video(
    job_id: str,
    api_key: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> FileResponse:
    require_api_key_header_or_query(x_api_key=x_api_key, api_key=api_key)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Video not ready yet")
    path = manager.resolve_video_path(job_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Video file missing")
    media = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/dev/")
@app.get("/dev")
def dev_ui() -> FileResponse:
    if not DEV_UI:
        raise HTTPException(
            status_code=404,
            detail="Dev UI disabled. Set DEV_UI=1 in .env for local testing only.",
        )
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Dev UI file missing")
    return FileResponse(index)


if STATIC_DIR.exists():
    app.mount("/dev/static", StaticFiles(directory=str(STATIC_DIR)), name="dev-static")
