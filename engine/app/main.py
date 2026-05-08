from __future__ import annotations

import asyncio
import hashlib
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
from fastapi.responses import JSONResponse, Response

from engine.app.models.schemas import (
    AdversaryGenerateRequest,
    AdversaryGenerateResponse,
    AdversaryInjectedError,
    AuditEvent,
    AuditEventsResponse,
    BatchItemError,
    BatchItemResult,
    BatchRollup,
    Claim,
    ClaimInput,
    IntegrityReport,
    ReportMetadata,
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
from engine.app.services.adversary import AdversaryAgent
from engine.app.services.anthropic_client import TokenLedger
from engine.app.services.audit import AuditLog, TraceStore, build_verify_record
from engine.app.services.cache import TTLCache, make_cache_key
from engine.app.services.metrics import MetricsRegistry
from engine.app.services.postgres import (
    PgAuditLog,
    PgCache,
    PgRateLimiter,
    PgTraceStore,
    PgBudgetStore,
    PgIdempotencyStore,
    PostgresStore,
    run_audit_offloader,
    run_pg_sweeper,
)
from engine.app.services.api_keys import ApiKeyService
from engine.app.services.r2 import R2Client
from engine.app.services.rate_limit import SlidingWindowRateLimiter
from engine.config import settings

logging.basicConfig(
    level=settings.engine_log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("trustlayer.engine")


_report_cache: "TTLCache[dict] | PgCache" = TTLCache(
    ttl_seconds=settings.cache_ttl_seconds,
    max_entries=settings.cache_max_entries,
)
_rate_limiter: "SlidingWindowRateLimiter | PgRateLimiter" = SlidingWindowRateLimiter(
    limit_per_minute=settings.rate_limit_per_minute
)
_metrics = MetricsRegistry()
# Default to the file/in-memory backends; lifespan swaps these in for the
# Postgres variants when DATABASE_URL is configured.
_audit_log: "AuditLog | PgAuditLog" = AuditLog(
    path=Path(settings.audit_path),
    max_bytes=settings.audit_max_bytes,
    enabled=settings.audit_enabled,
)
_trace_store: "TraceStore | PgTraceStore" = TraceStore(
    ttl_seconds=settings.trace_ttl_seconds,
    max_entries=settings.trace_max_entries,
    enabled=settings.trace_enabled,
)
_pg_store: Optional[PostgresStore] = None
_budget_store: Optional[PgBudgetStore] = None
_idempotency_store: Optional[PgIdempotencyStore] = None
_api_key_service: ApiKeyService = ApiKeyService(None)
_sweeper_task: Optional[asyncio.Task] = None
_offloader_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _audit_log, _trace_store, _pg_store, _sweeper_task, _offloader_task
    global _report_cache, _rate_limiter, _budget_store
    global _idempotency_store, _api_key_service

    log.info(
        "TrustLayer engine starting (env=%s, default=%s/%s)",
        settings.engine_env,
        settings.default_provider,
        settings.default_model,
    )

    if settings.database_url:
        try:
            _pg_store = PostgresStore(
                settings.database_url,
                min_size=settings.database_pool_min,
                max_size=settings.database_pool_max,
            )
            await _pg_store.connect()
            _audit_log = PgAuditLog(_pg_store, enabled=settings.audit_enabled)
            _trace_store = PgTraceStore(
                _pg_store,
                ttl_seconds=settings.trace_ttl_seconds,
                enabled=settings.trace_enabled,
            )
            _report_cache = PgCache(
                _pg_store,
                ttl_seconds=settings.cache_ttl_seconds,
            )
            _rate_limiter = PgRateLimiter(
                _pg_store,
                limit_per_minute=settings.rate_limit_per_minute,
                window_seconds=settings.rate_limit_window_seconds,
            )
            _budget_store = PgBudgetStore(_pg_store)
            _idempotency_store = PgIdempotencyStore(
                _pg_store, ttl_seconds=settings.idempotency_ttl_seconds
            )
            _api_key_service = ApiKeyService(_pg_store)

            _sweeper_task = asyncio.create_task(
                run_pg_sweeper(
                    _pg_store,
                    interval_seconds=settings.trace_sweeper_interval_seconds,
                    rate_window_seconds=settings.rate_limit_window_seconds,
                )
            )

            if settings.r2_account_id:
                r2 = R2Client(
                    account_id=settings.r2_account_id,
                    access_key_id=settings.r2_access_key_id,
                    secret_access_key=settings.r2_secret_access_key,
                    bucket_name=settings.r2_bucket_name,
                )
                _offloader_task = asyncio.create_task(
                    run_audit_offloader(
                        _audit_log,  # type: ignore[arg-type]
                        r2,
                        interval_seconds=settings.audit_offload_interval_seconds,
                        age_days=settings.audit_offload_age_days,
                    )
                )

            log.info(
                "Postgres-backed audit + trace + cache + rate-limit + budget + idempotency active."
            )
        except Exception as exc:  # noqa: BLE001 — never block startup on DB
            log.exception(
                "Failed to initialize Postgres storage; falling back to "
                "JSONL/in-memory backends. Error: %s",
                exc,
            )
            _pg_store = None

    # Initialize ApiKeyService even without DB (for legacy token support)
    if not _pg_store:
        _api_key_service = ApiKeyService(None)

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

    if _sweeper_task is not None:
        _sweeper_task.cancel()
        try:
            await _sweeper_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    if _offloader_task is not None:
        _offloader_task.cancel()
        try:
            await _offloader_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    if _pg_store is not None:
        await _pg_store.close()

    # Drain the Lobster Trap proxy connection pool (no-op when never used).
    from engine.app.services.lobstertrap import aclose_proxy_client
    await aclose_proxy_client()

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
    state = getattr(request, "state", None)
    api_key_id = getattr(state, "api_key_id", None) if state is not None else None
    if api_key_id:
        return api_key_id
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


def _required_scope(method: str, path: str) -> Optional[str]:
    """Return the scope required to call this route, or None if the route
    is unscoped (auth required but no granular gate).

    Scope vocabulary:
      - verify:write — POST any /verify*
      - verify:read  — GET  /verify/trace/{request_id}
      - audit:read   — GET  /audit/events
      - stats:read   — GET  /stats
      - "*"          — wildcard, grants any scope
    """
    method = method.upper()
    if path == "/stats" and method == "GET":
        return "stats:read"
    if path == "/audit/events" and method == "GET":
        return "audit:read"
    if path.startswith("/verify/trace/") and method == "GET":
        return "verify:read"
    if path.startswith("/verify") and method == "POST":
        return "verify:write"
    return None


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "unauthorized", "message": message},
        headers={"WWW-Authenticate": 'Bearer realm="trustlayer"'},
    )


def _forbidden_scope(required: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "error": "forbidden_scope",
            "message": (
                f"This API key is not authorized for {required!r}. "
                "Update the key's scopes to grant access."
            ),
            "required_scope": required,
        },
    )


# Registered FIRST so it ends up innermost in the user middleware stack —
# unhandled exceptions are caught here and the 500 response then flows back
# out through CORSMiddleware, picking up Access-Control-Allow-Origin headers.
# (Routing Exception via @app.exception_handler hits Starlette's outermost
# ServerErrorMiddleware, which bypasses user middleware and strips CORS.)
def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Walk single-exception ExceptionGroups down to the underlying error.

    Starlette's BaseHTTPMiddleware on Python 3.11+ uses anyio task groups,
    which wrap propagated errors in BaseExceptionGroup. A plain
    `except RuntimeError` won't see through that wrapper, so we unwrap
    explicitly before deciding which response branch to take.
    """
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


@app.middleware("http")
async def catch_unhandled_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as raw_exc:
        exc = _unwrap_exception_group(raw_exc)
        if isinstance(exc, (RuntimeError, ValueError)):
            # These are usually "expected" pipeline failures (e.g. API quota,
            # malformed LLM response, schema rejection). Return them as 500
            # but with the real message so the playground can show something
            # better than "unexpected error".
            log.warning(
                "Pipeline error on %s %s: %s",
                request.method,
                request.url.path,
                exc,
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "pipeline_error", "message": str(exc)},
            )
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        # Surface the exception class + a truncated message so the playground
        # can show the *type* of failure (e.g. "ClientError: 404 model not
        # found") without leaking a full stack. The full traceback is in
        # server logs via log.exception() above.
        raw_msg = str(exc).strip()
        msg = raw_msg.splitlines()[0] if raw_msg else ""
        if len(msg) > 240:
            msg = msg[:237] + "..."
        detail = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "internal_error",
                "message": "An unexpected error occurred.",
                "detail": detail,
            },
        )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _requires_auth(request):
        header = request.headers.get("authorization") or ""
        scheme, _, bearer = header.partition(" ")
        if scheme.lower() != "bearer" or not bearer:
            # If API_AUTH_TOKEN is empty, we allow unauthenticated access in dev.
            if not settings.api_auth_token:
                request.state.api_key_id = "default"
                request.state.api_scopes = None
                return await call_next(request)
            return _unauthorized("Missing Bearer token.")

        key = await _api_key_service.resolve(bearer.strip())
        if not key:
            return _unauthorized("Invalid API token.")

        request.state.api_key_id = key.id
        request.state.api_scopes = key.scopes

        required = _required_scope(request.method, request.url.path)
        if required is not None and not key.has_scope(required):
            return _forbidden_scope(required)
    else:
        # Public endpoints don't strictly need a key_id, but stashing 'default'
        # ensures downstream metrics/limiter logic doesn't crash.
        request.state.api_key_id = "default"
        request.state.api_scopes = None

    return await call_next(request)


@app.middleware("http")
async def idempotency_middleware(request: Request, call_next):
    if (
        not _pg_store
        or request.method != "POST"
        or not request.url.path.startswith("/verify")
    ):
        return await call_next(request)

    idempotency_key = request.headers.get("idempotency-key")
    if not idempotency_key:
        return await call_next(request)

    api_key_id = getattr(request.state, "api_key_id", "default")

    # Read body to compute hash.
    body = await request.body()
    try:
        import json as _json
        data = _json.loads(body)
        canonical = _json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        request_hash = hashlib.sha256(canonical).digest()
    except Exception:  # noqa: BLE001
        request_hash = hashlib.sha256(body).digest()

    # Re-supply body for downstream.
    async def receive():
        return {"type": "http.request", "body": body}
    request._receive = receive

    # 1. Check existing.
    existing = await _idempotency_store.get(api_key_id, idempotency_key)
    if existing:
        if existing["request_hash"] != request_hash:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": "idempotency_key_reuse",
                    "message": "Idempotency key reused with a different request body.",
                },
            )

        status_code = existing["status_code"]
        if status_code is None:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": "ident_request_in_progress",
                    "message": "A request with this idempotency key is already in progress.",
                },
            )

        # Replay.
        return JSONResponse(
            status_code=status_code,
            content=existing["body"],
            headers={"Idempotent-Replayed": "true"},
        )

    # 2. Insert in-flight.
    created = await _idempotency_store.create_in_flight(
        api_key_id, idempotency_key, request_hash
    )
    if not created:
        # Race condition.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "idempotent_request_in_progress",
                "message": "A request with this idempotency key is already in progress.",
            },
        )

    try:
        response: Response = await call_next(request)

        if 200 <= response.status_code < 500:
            # We need the response body to save it.
            # Note: StreamingResponse would be harder to handle here.
            # Most of our responses are JSONResponse.
            if hasattr(response, "body"):
                try:
                    import json as _json
                    resp_body = _json.loads(response.body)
                    await _idempotency_store.save_resolved(
                        api_key_id, idempotency_key, response.status_code, resp_body
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif response.status_code >= 500:
            # Delete so client can retry.
            await _idempotency_store.delete(api_key_id, idempotency_key)

        return response
    except Exception:
        # On crash, delete so client can retry.
        await _idempotency_store.delete(api_key_id, idempotency_key)
        raise


@app.middleware("http")
async def budget_middleware(request: Request, call_next):
    if (
        not settings.budget_enabled
        or not _pg_store
        or request.method != "POST"
        or not request.url.path.startswith("/verify")
    ):
        return await call_next(request)

    api_key_id = getattr(request.state, "api_key_id", "default")

    # 1. Get caps.
    key_meta = await _api_key_service._get_metadata(api_key_id)
    usd_cap = key_meta.daily_usd_cap if key_meta else settings.default_daily_usd_cap
    token_cap = key_meta.daily_token_cap if key_meta else settings.default_daily_token_cap

    if usd_cap is None and token_cap is None:
        return await call_next(request)

    # 2. Atomic check-and-reserve. Without this, two concurrent requests on
    # the same key can both observe a fresh under-cap snapshot and both
    # proceed — burning past the cap by N requests' worth of cost. The
    # reservation is a single CTE that snapshots audit_events + active
    # reservations and inserts iff the new request still fits, so only one
    # of N concurrent callers wins when the cap is one request away.
    reservation_id, usd_total, tokens_total = await _budget_store.try_reserve(
        api_key_id,
        usd_cap=usd_cap,
        token_cap=token_cap,
        estimated_usd=settings.budget_reservation_usd,
        estimated_tokens=settings.budget_reservation_tokens,
        ttl_seconds=settings.budget_reservation_ttl_seconds,
    )

    if reservation_id is None:
        reset_seconds = 3600  # Coarse reset estimate: next hour.
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "budget_exceeded",
                "message": (
                    f"Daily budget of {usd_cap} USD / {token_cap} tokens exceeded."
                ),
            },
            headers={
                "Retry-After": str(reset_seconds),
                "X-Budget-USD-Limit": str(usd_cap or "unlimited"),
                "X-Budget-USD-Remaining": "0",
                "X-Budget-Tokens-Limit": str(token_cap or "unlimited"),
                "X-Budget-Tokens-Remaining": "0",
            },
        )

    try:
        response = await call_next(request)
    finally:
        # Always release — the audit row appended during the request now
        # carries the real spend, so the provisional hold has done its job.
        # Wrapped in try/except inside the store so a release failure
        # doesn't mask the underlying response.
        await _budget_store.release(reservation_id)

    # Add budget headers based on the snapshot the reservation observed —
    # callers see the committed view including their own in-flight cost.
    if usd_cap:
        response.headers["X-Budget-USD-Limit"] = str(usd_cap)
        response.headers["X-Budget-USD-Remaining"] = str(max(0.0, usd_cap - usd_total))
    if token_cap:
        response.headers["X-Budget-Tokens-Limit"] = str(token_cap)
        response.headers["X-Budget-Tokens-Remaining"] = str(max(0, token_cap - tokens_total))

    return response


_METRICS_RECORDED_PATHS = ("/verify",)


@app.middleware("http")
async def timing_and_rate_limit_middleware(request: Request, call_next):
    start = time.perf_counter()

    if (
        settings.rate_limit_enabled
        and request.method == "POST"
        and request.url.path.startswith("/verify")
    ):
        allowed, remaining, retry_after = await _rate_limiter.check(_client_key(request))
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


async def _cached_or_run(key: str):
    if not settings.cache_enabled:
        return None
    return await _report_cache.get(key)


async def _cache_put(key: str, report: IntegrityReport) -> None:
    if settings.cache_enabled:
        await _report_cache.set(key, report.model_dump(mode="json"))


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


async def _save_trace(
    *,
    endpoint: str,
    report: IntegrityReport,
    evidence: list[TraceClaimEvidence],
    api_key_id: Optional[str] = None,
) -> None:
    if not _trace_store.enabled:
        return
    trace = VerifyTrace(
        request_id=report.metadata.request_id,
        endpoint=endpoint,
        report=report,
        evidence=list(evidence),
    )
    await _trace_store.save(trace, api_key_id=api_key_id)


async def _emit_audit_for_report(
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
    api_key_id = getattr(http_request.state, "api_key_id", "default")
    record = build_verify_record(
        request_id=report.metadata.request_id,
        api_key_id=api_key_id,
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
        security_risk_score=report.metadata.security_risk_score,
        security_intent_detected=report.metadata.security_intent_detected,
        security_intent_declared=report.metadata.security_intent_declared,
        security_action=report.metadata.security_action,
        security_intent_mismatch=report.metadata.security_intent_mismatch,
    )
    await _audit_log.append(record)


async def _emit_audit_for_batch(
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
    api_key_id = getattr(http_request.state, "api_key_id", "default")
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
    security_risk_score: Optional[str] = None
    security_intent_mismatch = False
    security_action: Optional[str] = None
    risk_rank = {"Low": 1, "Medium": 2, "High": 3}

    for item in results:
        if item.report is None:
            continue
        if batch_model == settings.default_model:
            batch_model = item.report.metadata.model
        meta = item.report.metadata
        if meta.security_risk_score and (
            risk_rank.get(meta.security_risk_score, 0)
            > risk_rank.get(security_risk_score or "", 0)
        ):
            security_risk_score = meta.security_risk_score
        if meta.security_intent_mismatch:
            security_intent_mismatch = True
        if meta.security_action and not security_action:
            security_action = meta.security_action

    record = build_verify_record(
        request_id=batch_id,
        api_key_id=api_key_id,
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
        security_risk_score=security_risk_score,
        security_intent_mismatch=security_intent_mismatch,
        security_action=security_action,
    )
    record["item_count"] = rollup.item_count
    record["ok_count"] = rollup.ok_count
    record["error_count"] = rollup.error_count
    record["hallucination_item_count"] = rollup.hallucination_item_count
    record["mode"] = rollup.mode
    await _audit_log.append(record)


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
            "/adversary/generate",
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
            "size": await _report_cache.get_size(),
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
            "size": await _report_cache.get_size(),
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
    http_request: Request,
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
    api_key_id = getattr(http_request.state, "api_key_id", None)
    records = await _audit_log.read_tail(
        limit=capped, since=since_dt, endpoint=endpoint, api_key_id=api_key_id
    )
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
async def verify_trace(request_id: str, http_request: Request) -> VerifyTrace:
    api_key_id = getattr(http_request.state, "api_key_id", None)
    trace = await _trace_store.get(request_id, api_key_id=api_key_id)
    if trace is None:
        # Mismatched-tenant lookups also land here so we don't disclose that
        # the request_id exists for some other key.
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
    cached = await _cached_or_run(key)
    if cached is not None:
        report = IntegrityReport.model_validate(cached)
        _stash_usage(http_request, report)
        await _emit_audit_for_report(http_request, endpoint="/verify", report=report)
        return report

    pipeline = VerifyPipeline()
    report = await pipeline.run(request)
    await _cache_put(key, report)
    _stash_usage(http_request, report)
    await _save_trace(
        endpoint="/verify",
        report=report,
        evidence=pipeline.last_evidence,
        api_key_id=getattr(http_request.state, "api_key_id", None),
    )
    await _emit_audit_for_report(http_request, endpoint="/verify", report=report)
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
    cached = await _cached_or_run(key)
    if cached is not None:
        report = IntegrityReport.model_validate(cached)
        _stash_usage(http_request, report)
        await _emit_audit_for_report(
            http_request, endpoint="/verify/quick", report=report
        )
        return report

    pipeline = VerifyPipeline()
    report = await pipeline.run_quick(request)
    await _cache_put(key, report)
    _stash_usage(http_request, report)
    await _save_trace(
        endpoint="/verify/quick",
        report=report,
        evidence=pipeline.last_evidence,
        api_key_id=getattr(http_request.state, "api_key_id", None),
    )
    await _emit_audit_for_report(http_request, endpoint="/verify/quick", report=report)
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
    cached = await _cached_or_run(key)
    if cached is not None:
        report = IntegrityReport.model_validate(cached)
        _stash_usage(http_request, report)
        await _emit_audit_for_report(
            http_request, endpoint="/verify/claims", report=report
        )
        return report

    pipeline = VerifyPipeline()
    report = await pipeline.run_with_claims(
        request.source_context,
        claims,
        provider=request.provider,
        model=request.model,
    )
    await _cache_put(key, report)
    _stash_usage(http_request, report)
    await _save_trace(
        endpoint="/verify/claims",
        report=report,
        evidence=pipeline.last_evidence,
        api_key_id=getattr(http_request.state, "api_key_id", None),
    )
    await _emit_audit_for_report(http_request, endpoint="/verify/claims", report=report)
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
    api_key_id = getattr(http_request.state, "api_key_id", None)
    results = await asyncio.gather(
        *(
            _process_batch_item(
                idx,
                item,
                mode,
                semaphore,
                provider=request.provider,
                model=request.model,
                api_key_id=api_key_id,
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
    await _emit_audit_for_batch(
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
    api_key_id: Optional[str] = None,
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
            cached = await _cached_or_run(key)
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
                await _cache_put(key, report)
                await _save_trace(
                    endpoint="/verify/batch",
                    report=report,
                    evidence=pipeline.last_evidence,
                    api_key_id=api_key_id,
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


@app.post(
    "/adversary/generate",
    response_model=AdversaryGenerateResponse,
    tags=["adversary"],
    summary="Generate an adversarial summary with subtle hallucinations.",
    description=(
        "Uses an LLM agent to generate a summary that purposefully includes "
        "subtle, believable hallucinations and contradictions. Useful for "
        "stress-testing the TrustLayer's detection limits."
    ),
)
async def adversary_generate(
    request: AdversaryGenerateRequest, http_request: Request
) -> AdversaryGenerateResponse:
    _enforce_size(request.source_context)
    ledger = TokenLedger()
    agent = AdversaryAgent(
        provider=request.provider,
        model=request.model,
        ledger=ledger,
    )

    from engine.app.services import llm_factory
    resolved = llm_factory.resolve(provider=request.provider, model=request.model)

    metadata = ReportMetadata(
        provider=resolved.provider,
        model=resolved.model,
    )
    request_id = metadata.request_id

    t0 = time.perf_counter()
    output = await agent.generate_adversarial_summary(request.source_context, request_id=request_id)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    metadata.duration_ms = duration_ms
    from engine.app.services.pricing import estimate_cost_usd
    metadata.input_tokens = ledger.usage.input_tokens
    metadata.output_tokens = ledger.usage.output_tokens
    metadata.total_tokens = ledger.usage.total_tokens
    metadata.estimated_cost_usd = round(
        estimate_cost_usd(
            metadata.model,
            ledger.usage.input_tokens,
            ledger.usage.output_tokens,
            cache_read_tokens=ledger.usage.cache_read_input_tokens,
        ),
        6,
    )

    # We don't audit adversary calls by default to keep the audit log focused
    # on verification results, but we could if needed.

    return AdversaryGenerateResponse(
        summary=output.summary,
        injections=[
            AdversaryInjectedError(
                type=inj.type,
                injected_claim=inj.injected_claim,
                original_fact=inj.original_fact,
                reasoning=inj.reasoning
            ) for inj in output.injections
        ],
        metadata=metadata
    )
