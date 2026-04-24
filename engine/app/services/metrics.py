from __future__ import annotations

import math
from collections import defaultdict, deque
from threading import Lock
from time import monotonic
from typing import Any


class EndpointMetrics:
    """Sliding-sample recorder for one endpoint.

    Keeps the last `window_size` latency samples and running counters for
    requests, errors, and token/cost totals. All operations are O(1) amortized
    and guarded by a lock so middleware can call `record` from concurrent
    request handlers safely.
    """

    __slots__ = (
        "window_size",
        "_latencies",
        "_lock",
        "request_count",
        "error_count",
        "input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "last_request_at",
    )

    def __init__(self, window_size: int = 1024) -> None:
        self.window_size = max(16, int(window_size))
        self._latencies: deque[float] = deque(maxlen=self.window_size)
        self._lock = Lock()
        self.request_count = 0
        self.error_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost_usd = 0.0
        self.last_request_at: float | None = None

    def record(
        self,
        *,
        latency_ms: float,
        error: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        with self._lock:
            self._latencies.append(latency_ms)
            self.request_count += 1
            if error:
                self.error_count += 1
            self.input_tokens += max(0, int(input_tokens))
            self.output_tokens += max(0, int(output_tokens))
            self.estimated_cost_usd += max(0.0, float(estimated_cost_usd))
            self.last_request_at = monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = sorted(self._latencies)
            count = len(samples)
            percentiles = _percentiles(samples, (50, 95, 99)) if count else {}
            return {
                "request_count": self.request_count,
                "error_count": self.error_count,
                "error_rate": (
                    self.error_count / self.request_count
                    if self.request_count
                    else 0.0
                ),
                "samples_in_window": count,
                "latency_ms": {
                    "p50": percentiles.get(50),
                    "p95": percentiles.get(95),
                    "p99": percentiles.get(99),
                    "min": samples[0] if samples else None,
                    "max": samples[-1] if samples else None,
                },
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            }

    def reset(self) -> None:
        with self._lock:
            self._latencies.clear()
            self.request_count = 0
            self.error_count = 0
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost_usd = 0.0
            self.last_request_at = None


def _percentiles(sorted_samples: list[float], ps: tuple[int, ...]) -> dict[int, float]:
    """Nearest-rank percentile — simple and sufficient for a demo dashboard."""
    out: dict[int, float] = {}
    n = len(sorted_samples)
    if n == 0:
        return out
    for p in ps:
        idx = max(0, min(n - 1, math.ceil(p / 100 * n) - 1))
        out[p] = round(sorted_samples[idx], 3)
    return out


class MetricsRegistry:
    """Per-endpoint metrics, plus global roll-up."""

    def __init__(self, window_size: int = 1024) -> None:
        self._window_size = window_size
        self._by_endpoint: dict[str, EndpointMetrics] = defaultdict(
            lambda: EndpointMetrics(window_size=self._window_size)
        )
        self._lock = Lock()
        self.started_at = monotonic()

    def record(self, endpoint: str, **kwargs: Any) -> None:
        with self._lock:
            metric = self._by_endpoint[endpoint]
        metric.record(**kwargs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            endpoint_snapshots = {
                name: m.snapshot() for name, m in self._by_endpoint.items()
            }
        total_requests = sum(s["request_count"] for s in endpoint_snapshots.values())
        total_errors = sum(s["error_count"] for s in endpoint_snapshots.values())
        total_input = sum(s["input_tokens"] for s in endpoint_snapshots.values())
        total_output = sum(s["output_tokens"] for s in endpoint_snapshots.values())
        total_cost = sum(s["estimated_cost_usd"] for s in endpoint_snapshots.values())
        return {
            "uptime_seconds": round(monotonic() - self.started_at, 3),
            "total": {
                "request_count": total_requests,
                "error_count": total_errors,
                "error_rate": (
                    total_errors / total_requests if total_requests else 0.0
                ),
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "estimated_cost_usd": round(total_cost, 6),
                "estimated_cost_per_1k_requests_usd": (
                    round(total_cost / total_requests * 1000, 6)
                    if total_requests
                    else 0.0
                ),
            },
            "endpoints": endpoint_snapshots,
        }

    def reset(self) -> None:
        with self._lock:
            for m in self._by_endpoint.values():
                m.reset()
            self.started_at = monotonic()
