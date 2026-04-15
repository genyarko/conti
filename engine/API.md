# TrustLayer Engine — API Reference

Base URL (dev): `http://localhost:8000`

All endpoints accept and return JSON. Error responses share the shape:

```json
{ "error": "<slug>", "message": "<human description>", "details": [...] }
```

Error slugs: `validation_error` (422), `payload_too_large` (413),
`too_many_claims` (413), `rate_limited` (429), `internal_error` (500).

Rate limiting: each client IP gets `RATE_LIMIT_PER_MINUTE` (default 60)
`POST /verify*` calls per 60-second sliding window. Responses include
`X-RateLimit-Limit` / `X-RateLimit-Remaining`. 429 responses include
`Retry-After` (seconds).

Response caching: identical input payloads return a cached `IntegrityReport`
for `CACHE_TTL_SECONDS` (default 900). Disable via `CACHE_ENABLED=false`.

---

## `GET /health`

Liveness + configuration probe. Returns model, environment, and cache stats.

```json
{
  "status": "ok",
  "env": "development",
  "model": "claude-opus-4-6",
  "anthropic_configured": true,
  "cache": { "enabled": true, "size": 3, "hits": 12, "misses": 7 }
}
```

---

## `POST /verify`

Full pipeline: extract → ground → consistency → aggregate. Best choice when
you need hallucination detection.

**Request**

```json
{
  "source_context": "The Eiffel Tower is a wrought-iron lattice tower in Paris, France. It was completed in 1889 and stands 330 metres tall.",
  "llm_output": "The Eiffel Tower is in Paris and was built in 1889. It is made of solid gold."
}
```

**Response** — `IntegrityReport`

```json
{
  "overall_score": 62,
  "verified":   [ { "claim_id": "clm_…", "status": "verified",      "grounding_score": 96, "integrity_score": 94, "...": "..." } ],
  "uncertain":  [],
  "flagged":    [],
  "hallucinations": [
    {
      "claim_id": "clm_…",
      "status": "hallucination",
      "grounding_score": 12,
      "grounding_level": "ungrounded",
      "consistency_verdict": "inconsistent",
      "is_hallucination": true,
      "integrity_score": 23,
      "reasoning": "Grounding: … | Consistency: …"
    }
  ],
  "claims": [ { "id": "clm_…", "text": "…", "category": "factual" } ],
  "metadata": {
    "model": "claude-opus-4-6",
    "request_id": "req_…",
    "duration_ms": 2431,
    "extractor_ms": 612,
    "grounder_ms": 1340,
    "consistency_ms": 1340,
    "claim_count": 2
  }
}
```

## `POST /verify/quick`

Grounding-only fast path. Skips the consistency LLM stage — cheaper and
faster, but will **not** populate the `hallucinations` bucket (those require
the consistency layer). Use when you only need "is this supported by the
source?"

Same request and response shape as `/verify`.

## `POST /verify/claims`

Skip extraction and verify a pre-built list of atomic claims.

**Request**

```json
{
  "source_context": "The Eiffel Tower is a wrought-iron lattice tower in Paris, France. It was completed in 1889.",
  "claims": [
    { "text": "The Eiffel Tower is in Paris.",           "category": "factual" },
    { "text": "The Eiffel Tower was completed in 1889.", "category": "quantitative" },
    { "id": "caller-chosen-id",
      "text": "The tower is made of wrought iron.",
      "source_quote": "wrought-iron lattice tower",
      "category": "factual" }
  ]
}
```

`ClaimInput` fields:

| field          | type                                                              | required |
|----------------|-------------------------------------------------------------------|----------|
| `id`           | string                                                            | no (auto) |
| `text`         | string                                                            | yes |
| `source_quote` | string                                                            | no |
| `category`     | `factual` \| `interpretive` \| `recommendation` \| `quantitative` | no (default `factual`) |

Response is an `IntegrityReport` identical in shape to `/verify`.

---

## Status & category enums

- **`ClaimStatus`** — `verified` (grounding ≥ 90 + consistent), `uncertain`
  (grounding 70–89 or minor concern), `flagged` (grounding < 70 or
  contradicts source), `hallucination` (grounding < 50 AND not consistent —
  auto-removed from main buckets).
- **`GroundingLevel`** — `grounded` / `partially_grounded` / `ungrounded`.
- **`ConsistencyVerdict`** — `consistent` / `minor_concern` / `inconsistent`
  / `contradictory`.
- **`ClaimCategory`** — `factual` / `interpretive` / `recommendation` /
  `quantitative`.

## Tuning knobs (env vars)

| var                            | default          | purpose |
|--------------------------------|------------------|---------|
| `ANTHROPIC_MODEL`              | claude-opus-4-6  | model used for reasoning stages |
| `ANTHROPIC_FAST_MODEL`         | claude-haiku-4-5 | model used for extraction/grounding |
| `GROUNDING_THRESHOLD_VERIFIED` | 90               | min score for `grounded` |
| `GROUNDING_THRESHOLD_PARTIAL`  | 70               | min score for `partially_grounded` |
| `HALLUCINATION_GROUNDING_MAX`  | 50               | grounding below this + not consistent → hallucination |
| `RATE_LIMIT_PER_MINUTE`        | 60               | per-IP cap on `/verify*` |
| `RATE_LIMIT_ENABLED`           | true             | disable for local load tests |
| `CACHE_ENABLED`                | true             | toggle the report cache |
| `CACHE_TTL_SECONDS`            | 900              | TTL for cached reports |
| `CACHE_MAX_ENTRIES`            | 512              | LRU cap |
| `MAX_INPUT_CHARS`              | 200000           | rejects oversize source+output (413) |
| `MAX_CLAIMS_PER_REQUEST`       | 200              | cap for `/verify/claims` (413) |

Interactive Swagger docs: **`/docs`** · OpenAPI JSON: **`/openapi.json`**.
