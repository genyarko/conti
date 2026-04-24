# TrustLayer Engine Benchmarks

This file is **generated** by `python bench/run.py`. Check it in after a run
so the numbers we quote in the pitch deck are reproducible from the commit.

## How to run

1. Start the engine locally with auth + rate-limit configured for benching:

   ```bash
   cd engine
   export ANTHROPIC_API_KEY=sk-ant-...
   export API_AUTH_TOKEN=bench-token
   export RATE_LIMIT_ENABLED=false   # bench will otherwise hit 429 at 10 req/min
   uvicorn engine.app.main:app --host 127.0.0.1 --port 8000
   ```

2. In a second shell, run the driver:

   ```bash
   python bench/run.py \
     --base-url http://127.0.0.1:8000 \
     --token    bench-token \
     --steady-n 10 --burst-n 10 --batch-n 10
   ```

3. Commit the updated `BENCHMARKS.md`.

## Scenarios

| scenario | what it measures | target endpoint |
|---|---|---|
| `single_steady` | baseline latency under sequential load | `POST /verify/quick` |
| `mixed_burst` | concurrent latency with mixed payload sizes | `POST /verify/quick` |
| `batch_throughput` | items/sec + `$/1k` under one bounded-concurrency call | `POST /verify/batch` |

All scenarios target `/verify/quick` (grounding-only). Each request has a
unique `[bench-<index>]` suffix in `llm_output` so the response cache does
not mask real latency or cost.

## Latest results

_Run `python bench/run.py` to populate this section. The driver overwrites
everything below this line._

<!-- BENCH-RESULTS -->

| scenario | n | wall (ms) | throughput (rps) | p50 (ms) | p95 (ms) | p99 (ms) | errors | tokens | cost (USD) | $/1k |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| single_steady    | — | — | — | — | — | — | — | — | — | — |
| mixed_burst      | — | — | — | — | — | — | — | — | — | — |
| batch_throughput | — | — | — | — | — | — | — | — | — | — |
