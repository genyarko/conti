# Hackathon Gap Closure Plan (Code-Level)

Date: 2026-04-24 (revised)

This plan answers: **"Do we need more code, and in what order, to score higher with judges?"**

## Short answer

**Yes — a focused set of additions will produce the numbers, screenshots, and narrative judges reward.** Everything below is sized for a hackathon timeline and biased toward visible demo payoff. Work that does not produce something a judge will read on stage has been cut.

## Guiding principles for this revision

- **Instrument before we benchmark.** Judges quote numbers, not k6 configs. The service itself has to produce p50/p95/p99, token totals, and $ estimates. The bench script just drives load against it.
- **Batch is the Big Data story.** `POST /verify/batch` doubles as the throughput test target. One deliverable covers the two gaps the fit assessment called out (Big Data + Scalability).
- **Cut tool adoption risk.** No Prometheus, no k6, no Locust, no scoped API key refactor. A JSON `/stats` endpoint, a 60-line `httpx` driver, and the existing bearer token are enough.
- **Reuse what's already instrumented.** `TTLCache.hits/misses` is exposed in `/health`; `ReportMetadata` already captures per-stage timings and `duration_ms`. We extend; we do not rebuild.

---

## Prioritized implementation order

### 1) Token + cost accounting in `ReportMetadata`  ← start here

**Why first:** everything downstream (stats endpoint, benchmark report, demo slide, governance audit) reads from this. It is also the single most enterprise-credible claim we can make on stage: "we can quote $/1k contract reviews."

**What to build**
- Extend `ReportMetadata` with `input_tokens`, `output_tokens`, `total_tokens`, `estimated_cost_usd`.
- Capture token usage from each Anthropic call (extractor, grounder, consistency) and sum into metadata.
- Add a per-model price table in `engine/config/settings.py` with input/output $/MTok rates; fall back to zero when model is unrecognized (log once).

**Files touched**
- `engine/app/models/schemas.py`
- `engine/app/pipeline/extractor.py`, `grounder.py`, `consistency.py`
- `engine/app/pipeline/orchestrator.py`
- `engine/config/settings.py`

**Acceptance**
- Every `IntegrityReport` response includes non-zero token + cost fields when an API call was made.
- Unit test asserts token/cost accumulation across a multi-claim fixture.

---

### 2) `/stats` JSON summary endpoint

**Why second:** the stats endpoint is the artifact judges will screenshot. It turns the accounting from step 1 into a live dashboard.

**What to build**
- In-process sliding-window recorder: per-endpoint request count, error count, latency samples.
- Derived percentiles: p50 / p95 / p99 from samples.
- Aggregate cache hit rate from existing `TTLCache` counters.
- Running totals: tokens, estimated cost.
- Expose at `GET /stats` (plain JSON, no auth needed — read-only aggregate of anonymized counters).

**Files touched**
- `engine/app/services/metrics.py` (new)
- `engine/app/main.py` (middleware hook + endpoint)

**Acceptance**
- `/stats` returns all fields with zero state when idle and realistic numbers after a batch run.
- p95 latency is present for `/verify`, `/verify/quick`, `/verify/batch`.

---

### 3) `POST /verify/batch` (async, bounded concurrency)

**Why third:** this is the Big Data deliverable *and* the load target for step 4. A single endpoint covers two gaps.

**What to build**
- Accept an array of `{source_context, llm_output}` items (reuse existing size/claim caps per item).
- Process with bounded concurrency (`asyncio.Semaphore`, configurable — start at 8).
- Return a single aggregate response: per-item report + roll-up (total items, pass/fail counts, total tokens, total cost, wall-clock time).
- Synchronous response for MVP (no job queue). Async job model is a stretch goal, explicitly deferred.

**Files touched**
- `engine/app/main.py`
- `engine/app/models/schemas.py` (`VerifyBatchRequest`, `VerifyBatchReport`)
- `engine/config/settings.py` (`batch_max_items`, `batch_concurrency`)

**Acceptance**
- 50-item batch completes without memory spike and populates the roll-up.
- Per-item failures do not abort the batch; they are reported in the response.

---

### 4) `bench/run.py` + `BENCHMARKS.md`

**Why fourth:** now that the service emits numbers and accepts batches, produce reproducible measurements.

**What to build**
- ~60-line async `httpx` driver in `bench/run.py`.
- Scenarios: single-item steady load, batch throughput, mixed-payload-size burst.
- Reads `/stats` before/after each scenario and writes a summary to `BENCHMARKS.md` (markdown table: throughput, p50/p95/p99, error rate, $/1k requests).

**Files touched**
- `bench/run.py` (new)
- `BENCHMARKS.md` (new, generated)

**Acceptance**
- `python bench/run.py` against a running local engine writes a committed `BENCHMARKS.md` we can quote in the deck.

---

### 5) Audit log (file-backed JSONL) + `GET /audit/events`

**Why fifth:** governance screenshot. Each verify call appends a line; the endpoint filters and returns.

**What to build**
- Append-only JSONL writer at `engine/app/services/audit.py`.
- Each record: `request_id`, timestamp, endpoint, model, claim counts, outcome counts, overall score, latency_ms, token totals, cost.
- `GET /audit/events?since=&endpoint=&limit=` returns filtered tail.
- Rotate by size (simple — one file, cap N MB, oldest-line drop).

**Files touched**
- `engine/app/services/audit.py` (new)
- `engine/app/main.py`
- `engine/app/models/schemas.py`

**Acceptance**
- Every `/verify*` call produces exactly one audit line with a stable `request_id` surfaced back in the response header.
- `GET /audit/events?limit=20` returns the 20 most recent records as JSON.

---

### 6) Explainability trace artifact  ✅ landed

**Why sixth:** "why was this accepted/blocked?" is the governance close. Same data we already compute; just expose it as a downloadable.

**What shipped**
- `GET /verify/trace/{request_id}` returns a `VerifyTrace` — the full `IntegrityReport` plus a per-claim `evidence` list with matched passage + location, grounding reasoning, fast-vs-semantic path, consistency verdict/reasoning, confidence, and internal-contradiction links.
- Orchestrator now captures per-claim evidence on each `run*()` and exposes it as `pipeline.last_evidence`; `main.py` persists it after each `/verify*` call.
- In-memory `TraceStore` in `engine/app/services/audit.py` (bounded TTL + LRU cap via `trace_ttl_seconds` / `trace_max_entries` / `trace_enabled`). 404s on unknown or expired IDs.
- Batch path saves one trace per item, keyed by the per-item `request_id` surfaced in each `BatchItemResult.report`.

**Files touched**
- `engine/app/pipeline/orchestrator.py` (retain trace material)
- `engine/app/services/audit.py` (TraceStore)
- `engine/app/main.py` (endpoint + per-verify save hooks)
- `engine/app/models/schemas.py` (`TraceClaimEvidence`, `VerifyTrace`)
- `engine/config/settings.py` (trace settings)
- `engine/tests/test_verify_trace.py`

**Acceptance**
- UI can link to `/verify/trace/{id}` and render the underlying evidence for any demoed request. Covered by `test_verify_trace.py` (7 tests: per-endpoint trace persistence, batch per-item, 404 on unknown id, 404 when store disabled, LRU eviction).

---

## Explicitly deferred (not in scope for hackathon)

- Prometheus `/metrics` format — `/stats` JSON is sufficient for demo.
- k6 / Locust — a Python `httpx` driver avoids tool adoption.
- Scoped API keys (`verify:read`, `verify:write`, `audit:read`, `admin:policy`) — multi-day auth refactor, zero demo payoff. Keep existing single bearer token.
- Policy enforcement engine (reject-if-score<X rules) — no user will toggle this during the 3-minute demo.
- Persistent results store / warehouse / Kafka sinks — in-memory + append-only JSONL covers the narrative.
- Async job model for `/verify/batch` — synchronous response is enough for MVP.

---

## Minimum demo-ready subset (if time collapses)

Ship steps **1, 2, 3**. With those alone the pitch is:

- "Here is `/stats`: p95 under batch load, cache hit rate, tokens consumed, estimated cost."
- "Here is `/verify/batch` processing N documents in one call."
- "Here is the cost per 1k contract reviews, measured not estimated."

Steps 4–6 are additive demo depth, not preconditions.

---

## Demo narrative after these changes

- "We don't just detect hallucinations — we provide **governed, measurable AI reliability at scale**."
- "Here is our p95 under load, cache hit rate, and measured cost envelope — from `/stats`, not a slide."
- "Here is the same verification system running in batch over N records in one call."
- "Here is the audit trail and per-request explainability trace for every verification."
