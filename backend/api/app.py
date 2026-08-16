"""Production FastAPI — Nova hook analyzer + website reel discover."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config.env import BACKEND_DIR  # loads .env
from discover.jobs import discover_manager
from hooks.jobs import manager
from platforms import platform_specs, validate_platforms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hook-api")

DEV_UI = (os.getenv("DEV_UI") or "1").strip().lower() in {"1", "true", "yes", "on"}
API_KEY = (os.getenv("API_KEY") or "").strip()
STATIC_DIR = BACKEND_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "API starting DEV_UI=%s hooks_workers=%s discover_workers=%s",
        DEV_UI,
        manager.max_workers,
        discover_manager.max_workers,
    )
    yield
    manager.shutdown()
    discover_manager.shutdown()
    log.info("API shut down")


app = FastAPI(
    title="Reel Intelligence API",
    version="2.2.0",
    description="Nova-first reel hook analysis + website → multi-platform reel discover.",
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
    url: str = Field(..., description="Instagram or TikTok video URL")


class DiscoverRequest(BaseModel):
    url: str = Field(..., description="Business landing page URL")
    platforms: Optional[list[str]] = Field(
        default=None,
        description='Platforms to scrape, e.g. ["instagram","tiktok"]. Default: ["instagram"].',
    )


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
        "hooks": {
            "max_concurrent_jobs": manager.max_workers,
            "max_queue_size": manager.max_queue_size,
        },
        "discover": {
            "max_concurrent_jobs": discover_manager.max_workers,
            "max_queue_size": discover_manager.max_queue_size,
            "platforms": platform_specs(),
        },
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


@app.get("/v1/discover/platforms")
def list_discover_platforms(_: None = Depends(require_api_key)) -> dict[str, Any]:
    return {"platforms": platform_specs()}


@app.post("/v1/discover/analyze", response_model=AnalyzeAccepted)
def analyze_discover(
    body: DiscoverRequest, _: None = Depends(require_api_key)
) -> AnalyzeAccepted:
    try:
        platforms = validate_platforms(body.platforms)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        job = discover_manager.submit(body.url, platforms=platforms)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AnalyzeAccepted(job_id=job.job_id, status=job.status)


@app.get("/v1/discover/jobs/{job_id}")
def get_discover_job(job_id: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    job = discover_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


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
