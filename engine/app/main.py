from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from engine.app.models.schemas import (
    Claim,
    ClaimInput,
    IntegrityReport,
    VerifyClaimsRequest,
    VerifyQuickRequest,
    VerifyRequest,
)
from engine.app.pipeline.orchestrator import VerifyPipeline
from engine.app.services.cache import TTLCache, make_cache_key
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "TrustLayer engine starting (env=%s, model=%s)",
        settings.engine_env,
        settings.anthropic_model,
    )
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is not set — pipeline calls will fail.")
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
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _requires_auth(request: Request) -> bool:
    return request.method == "POST" and request.url.path.startswith("/verify")


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "unauthorized", "message": message},
        headers={"WWW-Authenticate": 'Bearer realm="trustlayer"'},
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
    return response


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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred.",
        },
    )


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


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "name": "TrustLayer Engine",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
        "endpoints": ["/verify", "/verify/quick", "/verify/claims"],
    }


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "env": settings.engine_env,
        "model": settings.anthropic_model,
        "anthropic_configured": bool(settings.anthropic_api_key),
        "cache": {
            "enabled": settings.cache_enabled,
            "size": len(_report_cache),
            "hits": _report_cache.hits,
            "misses": _report_cache.misses,
        },
    }


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
async def verify(request: VerifyRequest) -> IntegrityReport:
    _enforce_size(request.source_context, request.llm_output)
    key = make_cache_key("full", request.source_context, request.llm_output)
    cached = _cached_or_run(key)
    if cached is not None:
        return IntegrityReport.model_validate(cached)

    pipeline = VerifyPipeline()
    report = await pipeline.run(request)
    _cache_put(key, report)
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
async def verify_quick(request: VerifyQuickRequest) -> IntegrityReport:
    _enforce_size(request.source_context, request.llm_output)
    key = make_cache_key("quick", request.source_context, request.llm_output)
    cached = _cached_or_run(key)
    if cached is not None:
        return IntegrityReport.model_validate(cached)

    pipeline = VerifyPipeline()
    report = await pipeline.run_quick(request)
    _cache_put(key, report)
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
async def verify_claims(request: VerifyClaimsRequest) -> IntegrityReport:
    _enforce_size(request.source_context)
    _enforce_claim_count(len(request.claims))

    claims = [_to_claim(ci) for ci in request.claims]
    key = make_cache_key(
        "claims",
        request.source_context,
        "\n".join(f"{c.id}::{c.category.value}::{c.text}" for c in claims),
    )
    cached = _cached_or_run(key)
    if cached is not None:
        return IntegrityReport.model_validate(cached)

    pipeline = VerifyPipeline()
    report = await pipeline.run_with_claims(request.source_context, claims)
    _cache_put(key, report)
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
