from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Iterable, Optional

from engine.app.models.schemas import VerifyTrace

log = logging.getLogger(__name__)


class AuditLog:
    """Append-only JSONL audit writer with coarse size-based rotation.

    One file at `path`. When a write would push the file past `max_bytes`,
    the oldest lines are dropped until the file is back under ~70% of cap.
    Concurrent writes from FastAPI handlers are serialized through a lock.
    """

    _TRIM_TARGET_RATIO = 0.7

    def __init__(self, *, path: Path, max_bytes: int, enabled: bool = True) -> None:
        self._path = Path(path)
        self._max_bytes = max(1024, int(max_bytes))
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        if self._enabled:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled

    def append(self, record: dict[str, Any]) -> None:
        if not self._enabled:
            return
        record.setdefault("timestamp", _utc_now_iso())
        line = json.dumps(record, separators=(",", ":"), default=_json_default) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            try:
                with self._path.open("ab") as f:
                    f.write(encoded)
                if self._path.stat().st_size > self._max_bytes:
                    self._rotate_locked()
            except OSError as exc:  # pragma: no cover — disk failure path
                log.warning("audit log write failed: %s", exc)

    def read_tail(
        self,
        *,
        limit: int = 100,
        since: Optional[datetime] = None,
        endpoint: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not self._enabled or not self._path.exists():
            return []
        limit = max(1, min(int(limit), 10_000))
        out: deque[dict[str, Any]] = deque(maxlen=limit)
        try:
            f = self._path.open("r", encoding="utf-8")
        except OSError:
            return []
        with f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if endpoint and record.get("endpoint") != endpoint:
                    continue
                if since is not None:
                    ts = _parse_iso(record.get("timestamp"))
                    if ts is None or ts < since:
                        continue
                out.append(record)
        return list(out)

    def clear(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._path.write_bytes(b"")

    def _rotate_locked(self) -> None:
        target = int(self._max_bytes * self._TRIM_TARGET_RATIO)
        try:
            raw = self._path.read_bytes()
        except OSError:
            return
        if len(raw) <= target:
            return
        lines = raw.splitlines(keepends=True)
        total = sum(len(line) for line in lines)
        dropped = 0
        idx = 0
        while idx < len(lines) and total - dropped > target:
            dropped += len(lines[idx])
            idx += 1
        kept = b"".join(lines[idx:])
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(kept)
            os.replace(tmp_path, self._path)
        except OSError as exc:  # pragma: no cover — disk failure path
            log.warning("audit log rotation failed: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


class TraceStore:
    """In-memory TTL-bounded store for explainability traces.

    Keyed by `request_id`. Entries past `ttl_seconds` are evicted on access;
    total size is capped at `max_entries` (LRU-style, oldest entry drops).
    """

    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_entries: int,
        enabled: bool = True,
    ) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(1, int(max_entries))
        self._enabled = bool(enabled)
        self._data: OrderedDict[str, tuple[float, VerifyTrace]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def save(self, trace: VerifyTrace) -> None:
        if not self._enabled:
            return
        now = monotonic()
        with self._lock:
            self._data[trace.request_id] = (now, trace)
            self._data.move_to_end(trace.request_id)
            self._evict_locked(now)

    def get(self, request_id: str) -> Optional[VerifyTrace]:
        if not self._enabled:
            return None
        now = monotonic()
        with self._lock:
            entry = self._data.get(request_id)
            if entry is None:
                return None
            stored_at, trace = entry
            if now - stored_at > self._ttl:
                self._data.pop(request_id, None)
                return None
            self._data.move_to_end(request_id)
            return trace

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _evict_locked(self, now: float) -> None:
        # Drop anything past its TTL first.
        expired = [k for k, (t, _) in self._data.items() if now - t > self._ttl]
        for k in expired:
            self._data.pop(k, None)
        # Then cap total size; OrderedDict iteration order is insertion/move-to-end.
        while len(self._data) > self._max:
            self._data.popitem(last=False)


def build_verify_record(
    *,
    request_id: str,
    endpoint: str,
    model: str,
    status_code: int,
    latency_ms: int,
    overall_score: Optional[int],
    claim_count: int,
    outcome_counts: dict[str, int],
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    batch_id: Optional[str] = None,
    batch_index: Optional[int] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "request_id": request_id,
        "timestamp": _utc_now_iso(),
        "endpoint": endpoint,
        "model": model,
        "status_code": int(status_code),
        "latency_ms": int(latency_ms),
        "overall_score": overall_score,
        "claim_count": int(claim_count),
        "outcome_counts": dict(outcome_counts),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(input_tokens) + int(output_tokens),
        "estimated_cost_usd": round(float(estimated_cost_usd), 6),
    }
    if batch_id is not None:
        record["batch_id"] = batch_id
    if batch_index is not None:
        record["batch_index"] = int(batch_index)
    if error is not None:
        record["error"] = error
    return record


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
