"""In-memory job store + bounded ThreadPoolExecutor for hook analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from config.env import env_int
from hooks.download import DOWNLOADS_DIR
from hooks.pipeline import analyze_reel_url
from platforms import detect_platform, media_key
from shared.jobs import InMemoryJobManager, utc_now

log = logging.getLogger("hook-jobs")


@dataclass
class Job:
    job_id: str
    url: str
    media_key: Optional[str] = None
    status: str = "queued"  # queued | running | completed | failed
    created_at: str = field(default_factory=utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None
    video_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
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


class JobManager(InMemoryJobManager[Job]):
    def __init__(
        self,
        max_workers: Optional[int] = None,
        max_queue_size: Optional[int] = None,
    ) -> None:
        super().__init__(
            max_workers=max_workers or env_int("MAX_CONCURRENT_JOBS", 3),
            max_queue_size=max_queue_size or env_int("MAX_QUEUE_SIZE", 20),
            thread_name_prefix="hook-worker",
            busy_label="Server",
        )
        self._inflight: dict[str, str] = {}

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
        key = media_key(cleaned)
        with self._lock:
            if key:
                existing_id = self._inflight.get(key)
                if existing_id:
                    existing = self._jobs.get(existing_id)
                    if existing is not None and existing.status in ("queued", "running"):
                        log.info(
                            "job %s coalesced key=%s url=%s",
                            existing_id,
                            key,
                            cleaned,
                        )
                        return existing
            if self._active_count() >= self.max_queue_size:
                raise RuntimeError(
                    f"{self._busy_label} busy: {self.max_queue_size} jobs already "
                    "queued/running. Retry later."
                )
            job_id = self.new_job_id()
            job = Job(job_id=job_id, url=cleaned, media_key=key)
            self._jobs[job_id] = job
            if key:
                self._inflight[key] = job_id

        self._executor.submit(self._run_job, job_id)
        log.info("job %s queued url=%s key=%s", job_id, cleaned, key)
        return job

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

    def _drop_inflight(self, job: Job) -> None:
        key = job.media_key
        if key and self._inflight.get(key) == job.job_id:
            self._inflight.pop(key, None)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "running"
            job.started_at = utc_now()
            url = job.url

        log.info("job %s running", job_id)
        try:
            result, video_path = analyze_reel_url(url, job_id=job_id)
            usage = result.get("usage") if isinstance(result, dict) else None
            cost = None
            if isinstance(result, dict):
                cost = result.get("cost_usd")
                if cost is None and isinstance(usage, dict):
                    cost = (usage.get("totals") or {}).get("combined_run_usd")
            with self._lock:
                job = self._jobs[job_id]
                job.status = "completed"
                job.finished_at = utc_now()
                job.result = result
                job.usage = usage if isinstance(usage, dict) else None
                job.cost_usd = float(cost) if cost is not None else None
                job.video_path = str(Path(video_path).resolve())
                self._drop_inflight(job)
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
                job.finished_at = utc_now()
                job.error = str(exc)
                self._drop_inflight(job)


manager = JobManager()
