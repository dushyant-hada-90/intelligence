"""Generic in-memory ThreadPoolExecutor job manager."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Generic, Optional, TypeVar

log = logging.getLogger("jobs-base")

T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryJobManager(Generic[T]):
    """Bounded queue + executor; subclasses implement create_job / run_job."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_queue_size: int,
        thread_name_prefix: str,
        busy_label: str = "Server",
    ) -> None:
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        self._busy_label = busy_label
        self._jobs: dict[str, T] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        log.info(
            "%s ready max_workers=%s max_queue_size=%s",
            thread_name_prefix,
            self.max_workers,
            self.max_queue_size,
        )

    def _active_count(self) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if getattr(j, "status", None) in ("queued", "running")
        )

    def new_job_id(self) -> str:
        return uuid.uuid4().hex

    def store_and_submit(self, job: T, job_id: str, runner: Callable[[str], None]) -> T:
        with self._lock:
            if self._active_count() >= self.max_queue_size:
                raise RuntimeError(
                    f"{self._busy_label} busy: {self.max_queue_size} jobs already "
                    "queued/running. Retry later."
                )
            self._jobs[job_id] = job
        self._executor.submit(runner, job_id)
        return job

    def get(self, job_id: str) -> Optional[T]:
        with self._lock:
            return self._jobs.get(job_id)

    def with_job(self, job_id: str, fn: Callable[[T], Any]) -> Any:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return fn(job)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
