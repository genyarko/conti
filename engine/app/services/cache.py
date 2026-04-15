from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from threading import Lock
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


def make_cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


class TTLCache(Generic[T]):
    """Bounded, thread-safe TTL cache with LRU eviction."""

    def __init__(self, *, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(1, int(max_entries))
        self._store: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[T]:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                self.misses += 1
                return None
            expires_at, value = item
            if expires_at < time.monotonic():
                self._store.pop(key, None)
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
