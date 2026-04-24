"""Benchmark driver for the TrustLayer Engine.

Runs three scenarios against a live engine, reads `/stats` before and after
each, and writes a reproducible `BENCHMARKS.md` summary next to the repo root.

Usage:
    python bench/run.py --base-url http://localhost:8000 [--token $API_TOKEN]

Notes:
    - Targets `/verify/quick` so that the pipeline skips the consistency LLM
      calls. Keeps runtime and Anthropic spend bounded for a hackathon bench.
    - A unique suffix is appended to each `llm_output` so every request is a
      cache miss; the response cache is still beneficial but will not mask
      throughput or latency numbers here.
    - If `RATE_LIMIT_ENABLED` is on (default), the engine caps at 10 req/min
      per client. The driver stops and prints the server's retry hint when
      it sees a 429. Set `RATE_LIMIT_ENABLED=false` on the engine to run a
      real throughput measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "BENCHMARKS.md"

SMALL = {
    "source_context": (
        "The Eiffel Tower is a wrought-iron lattice tower in Paris, France. "
        "It was completed in 1889 and stands 330 metres tall."
    ),
    "llm_output": "The Eiffel Tower is in Paris and was built in 1889.",
}

MEDIUM = {
    "source_context": (
        "Master Services Agreement dated 2025-01-15 between Alpha Corp and "
        "Beta LLC. Initial term: 36 months. Fees: USD 12,000 per month, net 30. "
        "Renewal: automatic 12-month extensions unless either party gives "
        "90 days prior written notice. Governing law: State of Delaware. "
        "Late payments accrue interest at 1.5% per month."
    ),
    "llm_output": (
        "Alpha Corp and Beta LLC entered a three-year services agreement at "
        "USD 12,000 per month, auto-renewing for twelve-month terms unless "
        "either side gives ninety days' written notice. Delaware law governs "
        "the contract."
    ),
}


def _payload(template: dict, index: int) -> dict:
    out = f"{template['llm_output']} [bench-{index}]"
    return {"source_context": template["source_context"], "llm_output": out}


def _batch_item(template: dict, index: int) -> dict:
    return _payload(template, index)


async def _get_json(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(path)
    r.raise_for_status()
    return r.json()


async def _post_verify_quick(client: httpx.AsyncClient, body: dict) -> tuple[float, int]:
    t0 = time.perf_counter()
    r = await client.post("/verify/quick", json=body)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if r.status_code == 429:
        raise SystemExit(
            "Engine returned 429 (rate limited). Set RATE_LIMIT_ENABLED=false "
            "on the engine and re-run."
        )
    r.raise_for_status()
    return elapsed_ms, r.status_code


def _percentile(samples: list[float], p: int) -> float | None:
    if not samples:
        return None
    s = sorted(samples)
    idx = max(0, min(len(s) - 1, -(-p * len(s) // 100) - 1))  # nearest-rank
    return round(s[idx], 2)


def _stats_delta(before: dict, after: dict) -> dict:
    b, a = before["total"], after["total"]
    return {
        "requests": a["request_count"] - b["request_count"],
        "errors": a["error_count"] - b["error_count"],
        "input_tokens": a["input_tokens"] - b["input_tokens"],
        "output_tokens": a["output_tokens"] - b["output_tokens"],
        "total_tokens": a["total_tokens"] - b["total_tokens"],
        "cost_usd": round(a["estimated_cost_usd"] - b["estimated_cost_usd"], 6),
    }


async def scenario_single_steady(client: httpx.AsyncClient, n: int) -> dict:
    latencies: list[float] = []
    t0 = time.perf_counter()
    for i in range(n):
        ms, _ = await _post_verify_quick(client, _payload(SMALL, i))
        latencies.append(ms)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return {"latencies_ms": latencies, "wall_ms": wall_ms}


async def scenario_mixed_burst(client: httpx.AsyncClient, n: int) -> dict:
    async def one(i: int) -> float:
        tmpl = MEDIUM if i % 2 else SMALL
        ms, _ = await _post_verify_quick(client, _payload(tmpl, 1000 + i))
        return ms

    t0 = time.perf_counter()
    latencies = list(await asyncio.gather(*(one(i) for i in range(n))))
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return {"latencies_ms": latencies, "wall_ms": wall_ms}


async def scenario_batch_throughput(client: httpx.AsyncClient, n: int) -> dict:
    items = [_batch_item(SMALL, 2000 + i) for i in range(n)]
    body = {"mode": "quick", "items": items}
    t0 = time.perf_counter()
    r = await client.post("/verify/batch", json=body, timeout=None)
    if r.status_code == 429:
        raise SystemExit("Engine returned 429 on /verify/batch — disable rate limiting and retry.")
    r.raise_for_status()
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return {"rollup": r.json()["rollup"], "wall_ms": wall_ms, "item_count": n}


def _summarize(name: str, before: dict, after: dict, result: dict) -> dict:
    latencies = result.get("latencies_ms") or []
    wall_ms = result["wall_ms"]
    n = len(latencies) if latencies else result.get("item_count", 0)
    throughput = round(n * 1000.0 / wall_ms, 2) if wall_ms and n else None
    summary = {
        "name": name,
        "n": n,
        "wall_ms": round(wall_ms, 2),
        "throughput_rps": throughput,
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "delta": _stats_delta(before, after),
    }
    if "rollup" in result:
        summary["rollup"] = result["rollup"]
    return summary


def _cost_per_1k(delta: dict, n: int) -> float | None:
    if not n:
        return None
    return round(delta["cost_usd"] * 1000.0 / n, 6)


def _render_markdown(health: dict, scenarios: list[dict]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# TrustLayer Engine Benchmarks",
        "",
        f"- **Generated:** {ts}",
        f"- **Model:** `{health.get('model', 'unknown')}`",
        f"- **Environment:** `{health.get('env', 'unknown')}`",
        "",
        "Produced by `python bench/run.py` — see that script for scenario definitions.",
        "Numbers below come from the engine's `/stats` endpoint (deltas before/after each scenario) combined with client-side wall-clock timings.",
        "",
        "## Summary",
        "",
        "| scenario | n | wall (ms) | throughput (rps) | p50 (ms) | p95 (ms) | p99 (ms) | errors | tokens | cost (USD) | $/1k |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in scenarios:
        d = s["delta"]
        per_1k = _cost_per_1k(d, s["n"])
        lines.append(
            "| {name} | {n} | {wall} | {tp} | {p50} | {p95} | {p99} | {err} | {tok} | {cost} | {per} |".format(
                name=s["name"],
                n=s["n"],
                wall=s["wall_ms"],
                tp=s["throughput_rps"] if s["throughput_rps"] is not None else "—",
                p50=s["p50_ms"] if s["p50_ms"] is not None else "—",
                p95=s["p95_ms"] if s["p95_ms"] is not None else "—",
                p99=s["p99_ms"] if s["p99_ms"] is not None else "—",
                err=d["errors"],
                tok=d["total_tokens"],
                cost=d["cost_usd"],
                per=per_1k if per_1k is not None else "—",
            )
        )
    lines += ["", "## Per-scenario detail", ""]
    for s in scenarios:
        lines += [f"### `{s['name']}`", "", "```json", json.dumps(s, indent=2), "```", ""]
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> None:
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=args.timeout
    ) as client:
        health = await _get_json(client, "/health")
        scenarios: list[dict] = []

        for name, coro in [
            ("single_steady", scenario_single_steady(client, args.steady_n)),
            ("mixed_burst", scenario_mixed_burst(client, args.burst_n)),
            ("batch_throughput", scenario_batch_throughput(client, args.batch_n)),
        ]:
            print(f"[bench] running {name} …", flush=True)
            before = await _get_json(client, "/stats")
            result = await coro
            after = await _get_json(client, "/stats")
            scenarios.append(_summarize(name, before, after, result))

    Path(args.out).write_text(_render_markdown(health, scenarios), encoding="utf-8")
    print(f"[bench] wrote {args.out}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--token", default=None, help="Bearer token if the engine requires auth")
    p.add_argument("--steady-n", type=int, default=10)
    p.add_argument("--burst-n", type=int, default=10)
    p.add_argument("--batch-n", type=int, default=10)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--out", default=str(DEFAULT_REPORT))
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(_run(_parse_args()))
