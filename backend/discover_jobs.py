"""In-memory job store + ThreadPoolExecutor for website → reel discover."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from discover_pipeline import run_discover
from platforms import validate_platforms
from tunables import DISCOVER_MAX_CONCURRENT_JOBS, DISCOVER_MAX_QUEUE_SIZE

log = logging.getLogger("discover-jobs")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DiscoverJob:
    job_id: str
    url: str
    platforms: list[str] = field(default_factory=lambda: ["instagram"])
    status: str = "queued"  # queued | running | completed | failed
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None
    cost_usd: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "url": self.url,
            "platforms": self.platforms,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
        }


class DiscoverJobManager:
    def __init__(
        self,
        max_workers: Optional[int] = None,
        max_queue_size: Optional[int] = None,
    ) -> None:
        self.max_workers = max_workers or DISCOVER_MAX_CONCURRENT_JOBS
        self.max_queue_size = max_queue_size or DISCOVER_MAX_QUEUE_SIZE
        self._jobs: dict[str, DiscoverJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="discover-worker",
        )
        log.info(
            "DiscoverJobManager ready max_workers=%s max_queue_size=%s",
            self.max_workers,
            self.max_queue_size,
        )

    def _active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))

    def validate_url(self, url: str) -> str:
        cleaned = (url or "").strip()
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Paste a full website URL, e.g. https://example.com/")
        return cleaned

    def submit(
        self, url: str, platforms: Optional[list[str]] = None
    ) -> DiscoverJob:
        cleaned = self.validate_url(url)
        selected = validate_platforms(platforms)
        with self._lock:
            if self._active_count() >= self.max_queue_size:
                raise RuntimeError(
                    f"Discover busy: {self.max_queue_size} jobs already queued/running. Retry later."
                )
            job_id = uuid.uuid4().hex
            job = DiscoverJob(job_id=job_id, url=cleaned, platforms=selected)
            self._jobs[job_id] = job

        self._executor.submit(self._run_job, job_id)
        log.info(
            "discover job %s queued url=%s platforms=%s", job_id, cleaned, selected
        )
        return job

    def get(self, job_id: str) -> Optional[DiscoverJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "running"
            job.started_at = _utc_now()
            url = job.url
            platforms = list(job.platforms)

        log.info("discover job %s running", job_id)
        try:
            result = run_discover(url, platforms=platforms)
            usage = result.get("usage") if isinstance(result, dict) else None
            cost = result.get("cost_usd") if isinstance(result, dict) else None
            with self._lock:
                job = self._jobs[job_id]
                job.status = "completed"
                job.finished_at = _utc_now()
                job.result = result
                job.usage = usage if isinstance(usage, dict) else None
                job.cost_usd = float(cost) if cost is not None else None
            log.info(
                "discover job %s completed cost_usd=%s reels=%s",
                job_id,
                cost,
                len((result or {}).get("reels") or []),
            )
        except Exception as exc:
            log.exception("discover job %s failed", job_id)
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.finished_at = _utc_now()
                job.error = str(exc)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


discover_manager = DiscoverJobManager()
