"""In-memory job store + bounded ThreadPoolExecutor for hook analysis."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hook_pipeline import DOWNLOADS_DIR, analyze_reel_url
from platforms import detect_platform

log = logging.getLogger("hook-jobs")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@dataclass
class Job:
    job_id: str
    url: str
    status: str = "queued"  # queued | running | completed | failed
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None
    video_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "url": self.url,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "cut_times": (self.result or {}).get("cut_times") if self.result else None,
            "video_url": (
                f"/v1/hooks/jobs/{self.job_id}/video"
                if self.status == "completed" and self.video_path
                else None
            ),
        }
        return payload


class JobManager:
    def __init__(
        self,
        max_workers: Optional[int] = None,
        max_queue_size: Optional[int] = None,
    ) -> None:
        self.max_workers = max_workers or _env_int("MAX_CONCURRENT_JOBS", 3)
        self.max_queue_size = max_queue_size or _env_int("MAX_QUEUE_SIZE", 20)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="hook-worker",
        )
        log.info(
            "JobManager ready max_workers=%s max_queue_size=%s",
            self.max_workers,
            self.max_queue_size,
        )

    def _active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))

    def validate_url(self, url: str) -> str:
        cleaned = (url or "").strip()
        if detect_platform(cleaned) is None:
            raise ValueError(
                "Paste a full Instagram Reel or TikTok video URL, e.g. "
                "https://www.instagram.com/reel/XXXX/ or "
                "https://www.tiktok.com/@user/video/123"
            )
        return cleaned

    def submit(self, url: str) -> Job:
        cleaned = self.validate_url(url)
        with self._lock:
            if self._active_count() >= self.max_queue_size:
                raise RuntimeError(
                    f"Server busy: {self.max_queue_size} jobs already queued/running. Retry later."
                )
            job_id = uuid.uuid4().hex
            job = Job(job_id=job_id, url=cleaned)
            self._jobs[job_id] = job

        self._executor.submit(self._run_job, job_id)
        log.info("job %s queued url=%s", job_id, cleaned)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def resolve_video_path(self, job_id: str) -> Optional[Path]:
        """Return the downloaded video Path if it belongs to this job and downloads dir."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or not job.video_path:
                return None
            raw = job.video_path
        path = Path(raw).resolve()
        downloads = DOWNLOADS_DIR.resolve()
        try:
            path.relative_to(downloads)
        except ValueError:
            log.warning("job %s video path outside downloads: %s", job_id, path)
            return None
        if not path.is_file():
            return None
        return path

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "running"
            job.started_at = _utc_now()
            url = job.url

        log.info("job %s running", job_id)
        try:
            result, video_path = analyze_reel_url(url)
            usage = result.get("usage") if isinstance(result, dict) else None
            cost = None
            if isinstance(result, dict):
                cost = result.get("cost_usd")
                if cost is None and isinstance(usage, dict):
                    cost = (usage.get("totals") or {}).get("combined_run_usd")
            with self._lock:
                job = self._jobs[job_id]
                job.status = "completed"
                job.finished_at = _utc_now()
                job.result = result
                job.usage = usage if isinstance(usage, dict) else None
                job.cost_usd = float(cost) if cost is not None else None
                job.video_path = str(Path(video_path).resolve())
            log.info(
                "job %s completed cost_usd=%s cuts=%s",
                job_id,
                cost,
                (result or {}).get("cut_times"),
            )
        except Exception as exc:
            log.exception("job %s failed", job_id)
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.finished_at = _utc_now()
                job.error = str(exc)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


manager = JobManager()
