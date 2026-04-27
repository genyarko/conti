from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine.app.models.schemas import (
    AuditEvent,
    AuditEventsResponse,
    BatchItemError,
    BatchItemResult,
    BatchRollup,
    Claim,
    ClaimInput,
    IntegrityReport,
    TraceClaimEvidence,
    VerifyBatchItem,
    VerifyBatchReport,
    VerifyBatchRequest,
    VerifyClaimsRequest,
    VerifyQuickRequest,
    VerifyRequest,
    VerifyTrace,
)
from engine.app.pipeline.orchestrator import VerifyPipeline
from engine.app.services.audit import AuditLog, TraceStore, build_verify_record
from engine.app.services.cache import TTLCache, make_cache_key
from engine.app.services.metrics import MetricsRegistry
from engine.app.services.rate_limit import SlidingWindowRateLimiter
from engine.config import settings

logging.basicConfig(
    level=settings.engine_log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("trustlayer.engine")


_report_cache: TTLCache[dict] = TTLCache(
    ttl_seconds=settings.cache_ttl_seconds,
    max_entries=settings.cache_max_entries,
)
_rate_limiter = SlidingWindowRateLimiter(
    limit_per_minute=settings.rate_limit_per_minute
)
_metrics = MetricsRegistry()
_audit_log = AuditLog(
    path=Path(settings.audit_path),
    max_bytes=settings.audit_max_bytes,
    enabled=settings.audit_enabled,
)
_trace_store = TraceStore(
    ttl_seconds=settings.trace_ttl_seconds,
    max_entries=settings.trace_max_entries,
    enabled=settings.trace_enabled,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "TrustLayer engine starting (env=%s, default=%s/%s)",
        settings.engine_env,
        settings.default_provider,
        settings.default_model,
    )
    google_configured = bool(settings.gemini_api_key) or (
        settings.gemini_use_vertex and bool(settings.gemini_project)
    )
    if not settings.anthropic_api_key and not google_configured:
        log.warning(
            "No provider is configured (ANTHROPIC_API_KEY / GEMINI_API_KEY / "
            "GEMINI_USE_VERTEX+GEMINI_PROJECT) — pipeline calls will fail."
        )
    elif not google_configured and settings.default_provider == "google":
        log.warning(
            "DEFAULT_PROVIDER=google but neither GEMINI_API_KEY nor "
            "GEMINI_USE_VERTEX+GEMINI_PROJECT is configured — "
            "default-provider verifications will fail."
        )
    elif not settings.anthropic_api_key and settings.default_provider == "anthropic":
        log.warning(
            "DEFAULT_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set — "
            "default-provider verifications will fail."
        )
    if not settings.api_auth_token:
        log.warning(
            "API_AUTH_TOKEN is not set — /verify endpoints are UNAUTHENTICATED. "
            "Do not deploy to a public URL without setting this."
        )
    yield
    log.info("TrustLayer engine shutting down.")


app = FastAPI(
    title="TrustLayer Engine",
    description=(
        "A general-purpose API that verifies LLM outputs for hallucinations, "
        "ungrounded claims, and logical inconsistencies.\n\n"
        "## Endpoints\n"
        "- `POST /verify` — full pipeline: extract → ground → consistency → aggregate.\n"
        "- `POST /verify/quick` — grounding-only fast path; skips LLM consistency calls.\n"
        "- `POST /verify/claims` — accepts pre-extracted claims; skips extraction.\n"
        "- `POST /verify/batch` — run many (source, output) pairs in one call with bounded concurrency.\n"
        "- `GET /stats` — live latency, throughput, token, and cost metrics.\n"
        "- `GET /audit/events` — tail of the append-only audit log.\n"
        "- `GET /verify/trace/{request_id}` — per-claim evidence trace for a prior verification.\n"
    ),
    version="0.2.0",
    lifespan=lifespan,
)

def _client_key(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd and (
        settings.trust_proxy_headers
        or client_host in set(settings.trusted_proxy_ips)
    ):
        return fwd.split(",")[0].strip()
    return client_host


def _requires_auth(request: Request) -> bool:
    path = request.url.path
    if path in {"/", "/health", "/docs", "/openapi.json", "/redoc"}:
        return False
    return path.startswith(("/verify", "/audit", "/stats"))


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "unauthorized", "message": message},
        headers={"WWW-Authenticate": 'Bearer realm="trustlayer"'},
    )


# Registered FIRST so it ends up innermost in the user middleware stack —
# unhandled exceptions are caught here and the 500 response then flows back
# out through CORSMiddleware, picking up Access-Control-Allow-Origin headers.
# (Routing Exception via @app.exception_handler hits Starlette's outermost
# ServerErrorMiddleware, which bypasses user middleware and strips CORS.)
@app.middleware("http")
async def catch_unhandled_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_error", "message": "An unexpected error occurred."},
        )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if settings.api_auth_token and _requires_auth(request):
        header = request.headers.get("authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return _unauthorized("Missing Bearer token.")
        if not hmac.compare_digest(presented.strip(), settings.api_auth_token):
            return _unauthorized("Invalid API token.")
    return await call_next(request)


_METRICS_RECORDED_PATHS = ("/verify",)


@app.middleware("http")
async def timing_and_rate_limit_middleware(request: Request, call_next):
    start = time.perf_counter()

    if (
        settings.rate_limit_enabled
        and request.method == "POST"
        and request.url.path.startswith("/verify")
    ):
        allowed, remaining, retry_after = _rate_limiter.check(_client_key(request))
        if not allowed:
            _record_metrics(request, start_perf=start, status_code=429, usage=None)
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": "rate_limited",
                    "message": (
                        f"Rate limit of {settings.rate_limit_per_minute} "
                        "requests per minute exceeded."
                    ),
                    "retry_after_seconds": round(retry_after, 2),
                },
                headers={
                    "Retry-After": str(max(1, int(retry_after) + 1)),
                    "X-RateLimit-Limit": str(settings.rate_limit_per_minute),
                    "X-RateLimit-Remaining": "0",
                },
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
    else:
        response = await call_next(request)

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    _record_metrics(
        request,
        start_perf=start,
        status_code=response.status_code,
        usage=getattr(request.state, "metrics_usage", None),
    )
    return response


# Registered last so it wraps auth + timing middlewares — short-circuit
# responses (e.g. 401, 429) still get Access-Control-Allow-Origin headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


def _record_metrics(
    request: Request,
    *,
    start_perf: float,
    status_code: int,
    usage: dict | None,
) -> None:
    path = request.url.path
    if request.method != "POST" or not path.startswith(_METRICS_RECORDED_PATHS):
        return
    latency_ms = (time.perf_counter() - start_perf) * 1000
    is_error = status_code >= 400
    _metrics.record(
        path,
        latency_ms=latency_ms,
        error=is_error,
        input_tokens=(usage or {}).get("input_tokens", 0),
        output_tokens=(usage or {}).get("output_tokens", 0),
        estimated_cost_usd=(usage or {}).get("estimated_cost_usd", 0.0),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": "Request payload failed validation.",
            "details": exc.errors(),
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        payload = exc.detail
    else:
        payload = {
            "error": _status_slug(exc.status_code),
            "message": str(exc.detail),
        }
    return JSONResponse(status_code=exc.status_code, content=payload)


def _status_slug(code: int) -> str:
    mapping = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        413: "payload_too_large",
        422: "validation_error",
        429: "rate_limited",
    }
    return mapping.get(code, "error")


def _enforce_size(source: str, output: str | None = None) -> None:
    limit = settings.max_input_chars
    total = len(source) + (len(output) if output else 0)
    if total > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "payload_too_large",
                "message": (
                    f"Combined source + output ({total} chars) exceeds "
                    f"max_input_chars={limit}."
                ),
            },
        )


def _enforce_claim_count(n: int) -> None:
    cap = settings.max_claims_per_request
    if n > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "too_many_claims",
                "message": f"Claim count {n} exceeds max_claims_per_request={cap}.",
            },
        )


def _cached_or_run(key: str):
    if not settings.cache_enabled:
        return None
    return _report_cache.get(key)


def _cache_put(key: str, report: IntegrityReport) -> None:
    if settings.cache_enabled:
        _report_cache.set(key, report.model_dump(mode="json"))


def _resolved_cache_parts(provider: Optional[str], model: Optional[str]) -> tuple[str, str]:
    """Pick the (provider, model) the pipeline will actually run on, so the
    cache key reflects what was computed. Without this, two callers asking
    for different models on the same input would see each other's results."""
    from engine.app.services.llm_factory import resolve as _resolve_llm

    resolved = _resolve_llm(provider=provider, model=model)
    return resolved.provider, resolved.model


def _stash_usage(http_request: Request, report: IntegrityReport) -> None:
    http_request.state.metrics_usage = {
        "input_tokens": report.metadata.input_tokens,
        "output_tokens": report.metadata.output_tokens,
        "estimated_cost_usd": report.metadata.estimated_cost_usd,
    }


def _outcome_counts(report: IntegrityReport) -> dict[str, int]:
    return {
        "verified": len(report.verified),
        "uncertain": len(report.uncertain),
        "flagged": len(report.flagged),
        "hallucinations": len(report.hallucinations),
    }


def _save_trace(
    *,
    endpoint: str,
    report: IntegrityReport,
    evidence: list[TraceClaimEvidence],
) -> None:
    if not _trace_store.enabled:
        return
    trace = VerifyTrace(
        request_id=report.metadata.request_id,
        endpoint=endpoint,
        report=report,
        evidence=list(evidence),
    )
    _trace_store.save(trace)


def _emit_audit_for_report(
    http_request: Request,
    *,
    endpoint: str,
    report: IntegrityReport,
) -> None:
    """Write one audit line for a single-report verify call and stash the
    request_id so the timing middleware surfaces X-Request-ID."""
    http_request.state.request_id = report.metadata.request_id
    if not _audit_log.enabled:
        return
    record = build_verify_record(
        request_id=report.metadata.request_id,
        endpoint=endpoint,
        model=report.metadata.model,
        status_code=200,
        latency_ms=report.metadata.duration_ms,
        overall_score=report.overall_score,
        claim_count=report.metadata.claim_count or len(report.claims),
        outcome_counts=_outcome_counts(report),
        input_tokens=report.metadata.input_tokens,
        output_tokens=report.metadata.output_tokens,
        estimated_cost_usd=report.metadata.estimated_cost_usd,
    )
    _audit_log.append(record)


def _emit_audit_for_batch(
    http_request: Request,
    *,
    batch_id: str,
    rollup: BatchRollup,
    results: list[BatchItemResult],
    duration_ms: int,
) -> None:
    """Write one audit line for the batch call (acceptance: exactly one line
    per /verify* call). Aggregated counts come from the rollup."""
    http_request.state.request_id = batch_id
    if not _audit_log.enabled:
        return
    outcome_counts = {"verified": 0, "uncertain": 0, "flagged": 0, "hallucinations": 0}
    claim_count = 0
    for item in results:
        if item.report is None:
            continue
        claim_count += item.report.metadata.claim_count or len(item.report.claims)
        for key in outcome_counts:
            outcome_counts[key] += len(getattr(item.report, key))
    # Pull the resolved model from the first OK report so the audit row
    # records what actually ran, not whatever ANTHROPIC_MODEL was set to.
    batch_model = settings.default_model
    for item in results:
        if item.report is not None:
            batch_model = item.report.metadata.model
            break
    record = build_verify_record(
        request_id=batch_id,
        endpoint="/verify/batch",
        model=batch_model,
        status_code=200,
        latency_ms=duration_ms,
        overall_score=None,
        claim_count=claim_count,
        outcome_counts=outcome_counts,
        input_tokens=rollup.total_input_tokens,
        output_tokens=rollup.total_output_tokens,
        estimated_cost_usd=rollup.estimated_cost_usd,
    )
    record["item_count"] = rollup.item_count
    record["ok_count"] = rollup.ok_count
    record["error_count"] = rollup.error_count
    record["hallucination_item_count"] = rollup.hallucination_item_count
    record["mode"] = rollup.mode
    _audit_log.append(record)


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "name": "TrustLayer Engine",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
        "endpoints": [
            "/verify",
            "/verify/quick",
            "/verify/claims",
            "/verify/batch",
            "/stats",
            "/audit/events",
            "/verify/trace/{request_id}",
        ],
    }


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "env": settings.engine_env,
        "default_provider": settings.default_provider,
        "default_model": settings.default_model,
        "providers": {
            "anthropic": {"configured": bool(settings.anthropic_api_key)},
            "google": {
                "configured": bool(settings.gemini_api_key)
                or (
                    settings.gemini_use_vertex
                    and bool(settings.gemini_project)
                ),
                "mode": "vertex" if settings.gemini_use_vertex else "ai-studio",
            },
        },
        "cache": {
            "enabled": settings.cache_enabled,
            "size": len(_report_cache),
            "hits": _report_cache.hits,
            "misses": _report_cache.misses,
        },
    }


@app.get(
    "/models",
    tags=["meta"],
    summary="Catalog of supported (provider, model) pairs for client UIs.",
    description=(
        "Returns the catalog of models the engine accepts on `/verify*` "
        "requests, with the per-provider availability flag (`available: false` "
        "when the provider's API key is unset). The frontend ModelSelector "
        "reads this list and greys out unusable options."
    ),
)
async def models() -> dict:
    from engine.app.services.models import list_models

    return {
        "default": {
            "provider": settings.default_provider,
            "model": settings.default_model,
        },
        "models": list_models(),
    }


@app.get(
    "/stats",
    tags=["meta"],
    summary="Live operational metrics (latency, throughput, tokens, cost).",
    description=(
        "Read-only aggregate counters for observability and benchmarking. "
        "Includes per-endpoint p50/p95/p99 latency, request/error totals, "
        "cache hit rate, token usage, and an estimated USD cost envelope."
    ),
)
async def stats() -> dict:
    total_hits = _report_cache.hits
    total_misses = _report_cache.misses
    lookups = total_hits + total_misses
    return {
        "default_provider": settings.default_provider,
        "default_model": settings.default_model,
        "cache": {
            "enabled": settings.cache_enabled,
            "size": len(_report_cache),
            "hits": total_hits,
            "misses": total_misses,
            "hit_rate": (total_hits / lookups) if lookups else 0.0,
        },
        "metrics": _metrics.snapshot(),
    }


_AUDIT_ALLOWED_ENDPOINTS = {
    "/verify",
    "/verify/quick",
    "/verify/claims",
    "/verify/batch",
}


@app.get(
    "/audit/events",
    response_model=AuditEventsResponse,
    tags=["meta"],
    summary="Tail the append-only audit log.",
    description=(
        "Returns the most recent audit records (newest last). Each `/verify*` "
        "call appends exactly one record — request_id, endpoint, model, "
        "latency, outcome counts, token totals, and cost. The same "
        "`request_id` is surfaced on the originating response's `X-Request-ID` "
        "header, so records can be correlated end-to-end."
    ),
)
async def audit_events(
    since: Optional[str] = Query(
        default=None,
        description="ISO-8601 timestamp; only records with `timestamp >= since` are returned.",
    ),
    endpoint: Optional[str] = Query(
        default=None,
        description="Filter to a single endpoint (e.g. `/verify`).",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        description="Maximum records to return. Capped by `audit_max_returned`.",
    ),
) -> AuditEventsResponse:
    since_dt: Optional[datetime] = None
    if since:
        since_dt = _parse_since_param(since)
    if endpoint is not None and endpoint not in _AUDIT_ALLOWED_ENDPOINTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_endpoint",
                "message": (
                    f"endpoint must be one of {sorted(_AUDIT_ALLOWED_ENDPOINTS)}."
                ),
            },
        )
    capped = min(int(limit), int(settings.audit_max_returned))
    records = _audit_log.read_tail(limit=capped, since=since_dt, endpoint=endpoint)
    return AuditEventsResponse(
        count=len(records),
        events=[AuditEvent.model_validate(r) for r in records],
    )


def _parse_since_param(value: str) -> datetime:
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_since",
                "message": "`since` must be an ISO-8601 timestamp (e.g. 2026-04-24T00:00:00Z).",
            },
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@app.get(
    "/verify/trace/{request_id}",
    response_model=VerifyTrace,
    tags=["verify"],
    summary="Retrieve the explainability trace for a prior verification.",
    description=(
        "Returns the full `IntegrityReport` plus the per-claim evidence the "
        "pipeline produced while computing it — matched passages, grounding "
        "reasoning, consistency verdicts and reasoning, and internal "
        "contradiction links. Use the `X-Request-ID` header from a prior "
        "`/verify*` response (or `request_id` in the audit log) to correlate. "
        "Traces are held in memory with a bounded TTL, so old request IDs may "
        "return 404."
    ),
    responses={
        200: {"description": "Trace for the given request_id."},
        404: {"description": "Trace not found or expired."},
    },
)
async def verify_trace(request_id: str) -> VerifyTrace:
    trace = _trace_store.get(request_id)
    if trace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "trace_not_found",
                "message": (
                    f"No trace is available for request_id={request_id!r}. "
                    "Traces expire after the configured TTL."
                ),
            },
        )
    return trace


_VERIFY_EXAMPLE = {
    "source_context": "The Eiffel Tower is a wrought-iron lattice tower in Paris, France. It was completed in 1889 and stands 330 metres tall.",
    "llm_output": "The Eiffel Tower is in Paris and was built in 1889. It is made of solid gold.",
}


@app.post(
    "/verify",
    response_model=IntegrityReport,
    tags=["verify"],
    summary="Verify an LLM output against a source context.",
    description=(
        "Runs the full pipeline — extract atomic claims, ground each against the "
        "source, evaluate source- and internal-consistency, and aggregate into a "
        "scored `IntegrityReport`. Best choice when you need hallucination "
        "detection as well as grounding."
    ),
    responses={
        200: {"description": "Integrity report."},
        413: {"description": "Input exceeds size limits."},
        422: {"description": "Validation error."},
        429: {"description": "Rate limit exceeded."},
    },
    openapi_extra={"requestBody": {"content": {"application/json": {"example": _VERIFY_EXAMPLE}}}},
)
async def verify(request: VerifyRequest, http_request: Request) -> IntegrityReport:
    _enforce_size(request.source_context, request.llm_output)
    provider, model = _resolved_cache_parts(request.provider, request.model)
    key = make_cache_key(
        "full", provider, model, request.source_context, request.llm_output
    )
    cached = _cached_or_run(key)
    if cached is not None:
        report = IntegrityReport.model_validate(cached)
        _stash_usage(http_request, report)
        _emit_audit_for_report(http_request, endpoint="/verify", report=report)
        return report

    pipeline = VerifyPipeline()
    report = await pipeline.run(request)
    _cache_put(key, report)
    _stash_usage(http_request, report)
    _save_trace(endpoint="/verify", report=report, evidence=pipeline.last_evidence)
    _emit_audit_for_report(http_request, endpoint="/verify", report=report)
    return report


@app.post(
    "/verify/quick",
    response_model=IntegrityReport,
    tags=["verify"],
    summary="Grounding-only verification (fast, cheap).",
    description=(
        "Skips the consistency LLM calls. Extracts claims and only checks "
        "whether each is grounded in the source. Returns the same "
        "`IntegrityReport` shape, but with no hallucinations bucket populated "
        "via consistency reasoning — only grounding thresholds drive status."
    ),
    openapi_extra={"requestBody": {"content": {"application/json": {"example": _VERIFY_EXAMPLE}}}},
)
async def verify_quick(
    request: VerifyQuickRequest, http_request: Request
) -> IntegrityReport:
    _enforce_size(request.source_context, request.llm_output)
    provider, model = _resolved_cache_parts(request.provider, request.model)
    key = make_cache_key(
        "quick", provider, model, request.source_context, request.llm_output
    )
    cached = _cached_or_run(key)
    if cached is not None:
        report = IntegrityReport.model_validate(cached)
        _stash_usage(http_request, report)
        _emit_audit_for_report(http_request, endpoint="/verify/quick", report=report)
        return report

    pipeline = VerifyPipeline()
    report = await pipeline.run_quick(request)
    _cache_put(key, report)
    _stash_usage(http_request, report)
    _save_trace(endpoint="/verify/quick", report=report, evidence=pipeline.last_evidence)
    _emit_audit_for_report(http_request, endpoint="/verify/quick", report=report)
    return report


_CLAIMS_EXAMPLE = {
    "source_context": "The Eiffel Tower is a wrought-iron lattice tower in Paris, France. It was completed in 1889.",
    "claims": [
        {"text": "The Eiffel Tower is in Paris.", "category": "factual"},
        {"text": "The Eiffel Tower was completed in 1889.", "category": "quantitative"},
    ],
}


@app.post(
    "/verify/claims",
    response_model=IntegrityReport,
    tags=["verify"],
    summary="Verify caller-supplied claims (skips extraction).",
    description=(
        "Use this when you already have atomic claims — whether from your own "
        "extractor, a structured generation pipeline, or a prior /verify call. "
        "Each claim is grounded and consistency-checked in parallel."
    ),
    openapi_extra={"requestBody": {"content": {"application/json": {"example": _CLAIMS_EXAMPLE}}}},
)
async def verify_claims(
    request: VerifyClaimsRequest, http_request: Request
) -> IntegrityReport:
    _enforce_size(request.source_context)
    _enforce_claim_count(len(request.claims))

    claims = [_to_claim(ci) for ci in request.claims]
    provider, model = _resolved_cache_parts(request.provider, request.model)
    key = make_cache_key(
        "claims",
        provider,
        model,
        request.source_context,
        "\n".join(f"{c.id}::{c.category.value}::{c.text}" for c in claims),
    )
    cached = _cached_or_run(key)
    if cached is not None:
        report = IntegrityReport.model_validate(cached)
        _stash_usage(http_request, report)
        _emit_audit_for_report(http_request, endpoint="/verify/claims", report=report)
        return report

    pipeline = VerifyPipeline()
    report = await pipeline.run_with_claims(
        request.source_context,
        claims,
        provider=request.provider,
        model=request.model,
    )
    _cache_put(key, report)
    _stash_usage(http_request, report)
    _save_trace(endpoint="/verify/claims", report=report, evidence=pipeline.last_evidence)
    _emit_audit_for_report(http_request, endpoint="/verify/claims", report=report)
    return report


def _to_claim(ci: ClaimInput) -> Claim:
    kwargs: dict = {
        "text": ci.text,
        "source_quote": ci.source_quote,
        "category": ci.category,
    }
    if ci.id:
        kwargs["id"] = ci.id
    return Claim(**kwargs)


_BATCH_EXAMPLE = {
    "mode": "full",
    "items": [
        {
            "source_context": "The Eiffel Tower is in Paris, France. It was completed in 1889.",
            "llm_output": "The Eiffel Tower is in Paris and opened in 1889.",
        },
        {
            "source_context": "Mount Everest is 8,848.86 metres tall.",
            "llm_output": "Mount Everest is 8,848.86 metres tall and located in Canada.",
        },
    ],
}


@app.post(
    "/verify/batch",
    response_model=VerifyBatchReport,
    tags=["verify"],
    summary="Verify many (source, output) pairs in one call.",
    description=(
        "Processes items concurrently with a bounded semaphore. Per-item "
        "failures are isolated — the batch always returns a result for each "
        "input. Response includes a roll-up with throughput, token, and cost "
        "totals suitable for load-test and `$/1k` reporting."
    ),
    responses={
        200: {"description": "Batch report with per-item results and roll-up."},
        413: {"description": "Too many items or an item exceeds per-item size limits."},
        422: {"description": "Validation error."},
        429: {"description": "Rate limit exceeded."},
    },
    openapi_extra={"requestBody": {"content": {"application/json": {"example": _BATCH_EXAMPLE}}}},
)
async def verify_batch(
    request: VerifyBatchRequest, http_request: Request
) -> VerifyBatchReport:
    _enforce_batch_count(len(request.items))
    for idx, item in enumerate(request.items):
        try:
            _enforce_size(item.source_context, item.llm_output)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            detail = {**detail, "item_index": idx}
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    concurrency = max(1, int(settings.batch_concurrency))
    semaphore = asyncio.Semaphore(concurrency)
    mode = request.mode

    t0 = time.perf_counter()
    results = await asyncio.gather(
        *(
            _process_batch_item(
                idx,
                item,
                mode,
                semaphore,
                provider=request.provider,
                model=request.model,
            )
            for idx, item in enumerate(request.items)
        )
    )
    duration_ms = int((time.perf_counter() - t0) * 1000)

    rollup = _build_rollup(results, duration_ms, concurrency, mode)
    report = VerifyBatchReport(rollup=rollup, results=results)

    http_request.state.metrics_usage = {
        "input_tokens": rollup.total_input_tokens,
        "output_tokens": rollup.total_output_tokens,
        "estimated_cost_usd": rollup.estimated_cost_usd,
    }
    batch_id = f"batch_{uuid4().hex[:12]}"
    _emit_audit_for_batch(
        http_request,
        batch_id=batch_id,
        rollup=rollup,
        results=results,
        duration_ms=duration_ms,
    )
    return report


def _enforce_batch_count(n: int) -> None:
    cap = settings.batch_max_items
    if n > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "too_many_items",
                "message": f"Batch size {n} exceeds batch_max_items={cap}.",
            },
        )


async def _process_batch_item(
    index: int,
    item: VerifyBatchItem,
    mode: str,
    semaphore: asyncio.Semaphore,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> BatchItemResult:
    async with semaphore:
        try:
            cache_provider, cache_model = _resolved_cache_parts(provider, model)
            key = make_cache_key(
                f"batch:{mode}",
                cache_provider,
                cache_model,
                item.source_context,
                item.llm_output,
            )
            cached = _cached_or_run(key)
            if cached is not None:
                report = IntegrityReport.model_validate(cached)
            else:
                pipeline = VerifyPipeline()
                if mode == "quick":
                    req = VerifyQuickRequest(
                        source_context=item.source_context,
                        llm_output=item.llm_output,
                        provider=provider,
                        model=model,
                    )
                    report = await pipeline.run_quick(req)
                else:
                    req = VerifyRequest(
                        source_context=item.source_context,
                        llm_output=item.llm_output,
                        provider=provider,
                        model=model,
                    )
                    report = await pipeline.run(req)
                _cache_put(key, report)
                _save_trace(
                    endpoint="/verify/batch",
                    report=report,
                    evidence=pipeline.last_evidence,
                )
            return BatchItemResult(index=index, status="ok", report=report)
        except Exception as exc:  # noqa: BLE001 — isolate per-item failures.
            log.exception("Batch item %d failed", index)
            return BatchItemResult(
                index=index,
                status="error",
                error=BatchItemError(
                    code=type(exc).__name__,
                    message="Batch item processing failed.",
                ),
            )


def _build_rollup(
    results: list[BatchItemResult],
    duration_ms: int,
    concurrency: int,
    mode: str,
) -> BatchRollup:
    ok = [r for r in results if r.status == "ok" and r.report is not None]
    errors = [r for r in results if r.status == "error"]
    total_in = sum(r.report.metadata.input_tokens for r in ok)
    total_out = sum(r.report.metadata.output_tokens for r in ok)
    total_cost = sum(r.report.metadata.estimated_cost_usd for r in ok)
    hallucination_items = sum(1 for r in ok if r.report.hallucinations)
    return BatchRollup(
        item_count=len(results),
        ok_count=len(ok),
        error_count=len(errors),
        hallucination_item_count=hallucination_items,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_tokens=total_in + total_out,
        estimated_cost_usd=round(total_cost, 6),
        duration_ms=duration_ms,
        concurrency=concurrency,
        mode=mode,
    )
