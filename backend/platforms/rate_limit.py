"""Jittered serialized rate limiter for platform SERP calls."""

from __future__ import annotations

import random
import threading
import time


class JitterLimiter:
    """Serialize calls with a random gap between min_delay and max_delay seconds."""

    def __init__(self, min_delay: float, max_delay: float) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._lock = threading.Lock()
        self._last_at = 0.0

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def wait(self) -> None:
        """Caller must hold self.lock."""
        lo = min(self.min_delay, self.max_delay)
        hi = max(self.min_delay, self.max_delay)
        delay = random.uniform(lo, hi)
        now = time.monotonic()
        wait = (self._last_at + delay) - now
        if wait > 0:
            time.sleep(wait)

    def mark(self) -> None:
        """Caller must hold self.lock."""
        self._last_at = time.monotonic()
