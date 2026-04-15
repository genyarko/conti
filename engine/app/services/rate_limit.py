from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Deque


class SlidingWindowRateLimiter:
    """In-memory per-key sliding-window rate limiter.

    Not suitable for multi-process deployments — swap for Redis in that case.
    """

    def __init__(self, *, limit_per_minute: int) -> None:
        self._limit = max(1, int(limit_per_minute))
        self._window_s = 60.0
        self._buckets: dict[str, Deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, int, float]:
        """Return (allowed, remaining, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self._window_s
        with self._lock:
            q = self._buckets.get(key)
            if q is None:
                q = deque()
                self._buckets[key] = q
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._limit:
                retry_after = max(0.0, q[0] + self._window_s - now)
                return False, 0, retry_after
            q.append(now)
            return True, self._limit - len(q), 0.0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
