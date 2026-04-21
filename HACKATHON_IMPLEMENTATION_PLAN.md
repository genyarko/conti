# Hackathon Gap Closure Plan (Code-Level)

Date: 2026-04-21

This plan answers: **"Do we need more code?"**

## Short answer

**Yes — if you want to score higher on Big Data + enterprise readiness, you should add targeted code.**

You already have a solid trust-verification product. The next lift is to make enterprise-scale and governance evidence **visible in the product itself**, not just in slides.

---

## 1) Big Data angle is underdeveloped

### What to build (minimum viable)

1. **Batch verification endpoint + async job model**
   - Add `POST /verify/batch` in `engine/app/main.py` to accept many `(source_context, llm_output)` items.
   - Add job status APIs (`GET /verify/batch/{job_id}`) with progressive results.
   - Add request/response schemas in `engine/app/models/schemas.py`.

2. **Streaming/NDJSON ingest path**
   - Support newline-delimited JSON payload processing to handle large input feeds.
   - Process in chunks and cap memory footprint with bounded queues.

3. **Storage-backed results (not only in-memory cache)**
   - Add a persistence adapter (`engine/app/services/store.py`) for results + metadata.
   - Keep current TTL cache, but persist batch outputs for replay/analytics.

4. **Data platform export hooks**
   - Add optional sink integration (CSV/JSONL file sink first; warehouse/Kafka as stretch).
   - Export per-claim outcome, latency, model, score buckets.

### Files likely touched

- `engine/app/main.py`
- `engine/app/models/schemas.py`
- `engine/app/pipeline/orchestrator.py`
- `engine/app/services/cache.py` (reuse)
- `engine/app/services/` (new `store.py`, `batch.py`)
- `engine/API.md`

### Acceptance criteria

- Can submit 100+ records in one request (or job) without memory spikes.
- Can retrieve job progress and final summarized report.
- Can export run outputs in machine-readable format.

---

## 2) Governance and enterprise deployment evidence

### What to build (minimum viable)

1. **Audit trail endpoint**
   - Log each verify/analyze call with:
     - `request_id`, timestamp, caller key/tenant, selected model
     - claim counts, outcome counts, overall score, latency
   - Add `GET /audit/events` with filters (time range, status, model).

2. **Policy enforcement layer**
   - Add request-time policy config (deny rules), e.g.:
     - reject if hallucination rate > X
     - reject if overall score < Y for high-risk categories
   - Return explicit policy verdict in response metadata.

3. **Role/API-key scopes**
   - Replace single bearer token model with scoped keys:
     - `verify:read`, `verify:write`, `audit:read`, `admin:policy`.

4. **Explainability artifact**
   - Add downloadable verification trace per request:
     - matched passages, reasoning snippets, bucket transitions.

### Files likely touched

- `engine/app/main.py` (auth + new endpoints)
- `engine/app/models/schemas.py` (audit/policy schemas)
- `engine/app/services/rate_limit.py` (per-key quotas)
- `engine/config/settings.py` (policy + key config)
- `demo-app/backend/app/main.py` (pass through user identity/model)
- `demo-app/frontend/src/views/*` + new governance view components

### Acceptance criteria

- Every request is queryable in an audit feed.
- Admin can set policy thresholds without code changes.
- UI shows “why accepted/blocked” for governance transparency.

---

## 3) Scalability proof points are implied, not benchmarked

### What to build (minimum viable)

1. **Load test harness in repo**
   - Add `bench/` with scripts (k6 or Locust) for `/verify` and `/verify/quick`.
   - Include scenarios: steady load, burst load, mixed payload sizes.

2. **Metrics endpoint and instrumentation**
   - Add counters/histograms:
     - request count, errors, cache hit rate, p50/p95/p99 latency
     - per-endpoint and per-model cost estimate
   - Expose via `/metrics` (Prometheus format) or JSON summary endpoint.

3. **SLO dashboard artifact**
   - Generate a simple markdown report from benchmark runs:
     - throughput, latency, error rate, cost per 1k requests.

4. **Cost estimator**
   - Add token/cost accounting in `ReportMetadata` + aggregate endpoint (`/stats/cost`).

### Files likely touched

- `engine/app/main.py` (metrics endpoints)
- `engine/app/models/schemas.py` (cost/metrics fields)
- `engine/app/pipeline/orchestrator.py` (timing + token accumulation)
- `engine/tests/` (benchmark smoke tests)
- new `bench/` folder and `BENCHMARKS.md`

### Acceptance criteria

- You can show benchmark numbers from reproducible scripts.
- You can quote concrete SLO targets and observed p95 latency.
- You can estimate operating cost for demo-scale and enterprise-scale runs.

---

## Prioritized implementation order (for hackathon timeline)

1. **Scalability evidence first (fastest judging impact):** benchmark harness + metrics endpoint.
2. **Governance second:** audit feed + policy verdict metadata.
3. **Big Data third:** batch/async ingest and export pipeline.

If time is very tight, do only this MVP subset:

- `/metrics` + `bench/` scripts + `BENCHMARKS.md`
- `request_id`-based audit log (file-backed) + `GET /audit/events`
- `POST /verify/batch` with in-process queue and chunked processing

---

## Demo narrative after these changes

- “We don’t just detect hallucinations — we provide **governed, measurable AI reliability at scale**.”
- “Here is our p95 under load, cache hit rate, and estimated cost envelope.”
- “Here is the audit trail and policy decision for each verification request.”
- “Here is the same verification system running in batch over large datasets.”
