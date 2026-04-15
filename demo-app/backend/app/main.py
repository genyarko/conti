from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.models.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    UploadResponse,
)
from app.services.contract_store import store
from app.services.ingest import ingest_bytes, ingest_text
from app.services.pipeline import AnalysisPipeline

logging.basicConfig(
    level=settings.demo_log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("trustlayer.demo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "Contract reviewer demo starting — trustlayer=%s anthropic_model=%s",
        settings.trustlayer_base_url,
        settings.anthropic_model,
    )
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is not set — /analyze will fail.")
    yield
    log.info("Contract reviewer demo shutting down.")


app = FastAPI(
    title="TrustLayer Contract Reviewer — Demo API",
    description=(
        "Backend for the Contract Reviewer demo. Accepts a contract upload, "
        "extracts clauses, runs a contract-analyst LLM pass, then verifies "
        "each finding against the clause text via the TrustLayer engine."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": "Request payload failed validation.",
            "details": exc.errors(),
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    payload = exc.detail if isinstance(exc.detail, dict) else {
        "error": "error",
        "message": str(exc.detail),
    }
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "message": "An unexpected error occurred."},
    )


@app.get("/", tags=["meta"])
async def root():
    return {
        "name": "TrustLayer Contract Reviewer — Demo API",
        "version": app.version,
        "docs": "/docs",
        "endpoints": ["/upload", "/analyze", "/samples", "/samples/{name}"],
    }


@app.get("/health", tags=["meta"])
async def health():
    return {
        "status": "ok",
        "trustlayer_base_url": settings.trustlayer_base_url,
        "anthropic_configured": bool(settings.anthropic_api_key),
        "contracts_in_store": len(store),
    }


@app.post(
    "/upload",
    response_model=UploadResponse,
    tags=["contracts"],
    summary="Upload a contract (PDF, DOCX, or plain text).",
)
async def upload_contract(
    file: Optional[UploadFile] = File(default=None),
    text: Optional[str] = Form(default=None),
    filename: Optional[str] = Form(default=None),
) -> UploadResponse:
    if file is None and not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bad_request",
                "message": "Provide either a `file` upload or a `text` form field.",
            },
        )

    if file is not None:
        content = await file.read()
        _enforce_upload_size(len(content))
        contract = ingest_bytes(content, filename=file.filename, content_type=file.content_type)
    else:
        assert text is not None
        _enforce_upload_size(len(text.encode("utf-8")))
        contract = ingest_text(text, filename=filename or "pasted.txt")

    _enforce_char_limit(len(contract.raw_text))

    store.put(contract)
    return UploadResponse(
        contract_id=contract.contract_id,
        filename=contract.filename,
        doc_type=contract.doc_type,
        num_clauses=len(contract.clauses),
        char_count=len(contract.raw_text),
        clauses=contract.clauses,
        raw_text=contract.raw_text,
    )


@app.post(
    "/analyze",
    response_model=AnalyzeResponse,
    tags=["contracts"],
    summary="Analyze a previously-uploaded contract (or raw text) end-to-end.",
    description=(
        "Runs the contract-analyst LLM pass and then verifies every finding "
        "against the clause text through the TrustLayer engine. Findings whose "
        "supporting claims are classified as hallucinations are moved to "
        "`removed_findings` so the UI can show a before/after comparison."
    ),
)
async def analyze_contract(request: AnalyzeRequest) -> AnalyzeResponse:
    if request.contract_id:
        contract = store.get(request.contract_id)
        if contract is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "message": f"No contract found with id {request.contract_id}. Upload again.",
                },
            )
    elif request.text:
        _enforce_char_limit(len(request.text))
        contract = ingest_text(request.text, filename=request.filename or "pasted.txt")
        store.put(contract)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bad_request",
                "message": "Provide either `contract_id` (from /upload) or `text`.",
            },
        )

    pipeline = AnalysisPipeline()
    return await pipeline.run(contract, skip_verification=request.skip_verification)


@app.get("/samples", tags=["samples"], summary="List bundled demo contracts.")
async def list_samples():
    samples_dir = settings.sample_contracts_dir
    if not samples_dir.exists():
        return {"samples": []}
    entries = []
    for path in sorted(samples_dir.glob("*.txt")):
        entries.append(
            {
                "name": path.stem,
                "filename": path.name,
                "size_bytes": path.stat().st_size,
            }
        )
    return {"samples": entries}


@app.get(
    "/samples/{name}",
    tags=["samples"],
    summary="Fetch the raw text of a bundled demo contract.",
)
async def get_sample(name: str):
    path = _sample_path(name)
    text = path.read_text(encoding="utf-8")
    return {"name": name, "filename": path.name, "text": text}


@app.post(
    "/samples/{name}/load",
    response_model=UploadResponse,
    tags=["samples"],
    summary="Load a bundled demo contract into the store for analysis.",
)
async def load_sample(name: str) -> UploadResponse:
    path = _sample_path(name)
    text = path.read_text(encoding="utf-8")
    contract = ingest_text(text, filename=path.name)
    store.put(contract)
    return UploadResponse(
        contract_id=contract.contract_id,
        filename=contract.filename,
        doc_type=contract.doc_type,
        num_clauses=len(contract.clauses),
        char_count=len(contract.raw_text),
        clauses=contract.clauses,
        raw_text=contract.raw_text,
    )


def _sample_path(name: str) -> Path:
    # Prevent path traversal: only allow a simple slug, then resolve in the samples dir.
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "bad_request", "message": "Invalid sample name."},
        )
    base = settings.sample_contracts_dir
    candidate = (base / f"{name}.txt").resolve()
    if not str(candidate).startswith(str(base.resolve())):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "bad_request", "message": "Invalid sample name."},
        )
    if not candidate.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Sample '{name}' not found."},
        )
    return candidate


def _enforce_upload_size(size: int) -> None:
    if size > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "payload_too_large",
                "message": (
                    f"Upload size {size} bytes exceeds max_upload_bytes="
                    f"{settings.max_upload_bytes}."
                ),
            },
        )


def _enforce_char_limit(chars: int) -> None:
    if chars > settings.max_contract_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "payload_too_large",
                "message": (
                    f"Contract length {chars} chars exceeds max_contract_chars="
                    f"{settings.max_contract_chars}."
                ),
            },
        )
