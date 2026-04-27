from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ClaimCategory(str, Enum):
    FACTUAL = "factual"
    INTERPRETIVE = "interpretive"
    RECOMMENDATION = "recommendation"
    QUANTITATIVE = "quantitative"


class GroundingLevel(str, Enum):
    GROUNDED = "grounded"
    PARTIALLY_GROUNDED = "partially_grounded"
    UNGROUNDED = "ungrounded"


class ConsistencyVerdict(str, Enum):
    CONSISTENT = "consistent"
    MINOR_CONCERN = "minor_concern"
    INCONSISTENT = "inconsistent"
    CONTRADICTORY = "contradictory"


class ClaimStatus(str, Enum):
    VERIFIED = "verified"
    UNCERTAIN = "uncertain"
    FLAGGED = "flagged"
    HALLUCINATION = "hallucination"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_context: str = Field(
        ...,
        min_length=1,
        description="The ground-truth source the LLM output should be checked against.",
    )
    llm_output: str = Field(
        ...,
        min_length=1,
        description="The LLM-generated text to verify.",
    )
    output_schema: Optional[dict[str, Any]] = Field(
        default=None,
        max_length=128,
        description="Optional JSON schema describing the expected structure of llm_output.",
    )
    provider: Optional[str] = Field(
        default=None,
        description=(
            "Optional provider override (e.g. 'google', 'anthropic'). "
            "Falls back to the engine's configured default."
        ),
    )
    model: Optional[str] = Field(
        default=None,
        description=(
            "Optional model id override (e.g. 'gemini-3.1-pro-preview'). "
            "Falls back to the provider's flagship model."
        ),
    )


class Claim(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("clm"))
    text: str = Field(..., description="The atomic claim extracted from the LLM output.")
    source_quote: Optional[str] = Field(
        default=None,
        description="The verbatim passage from source_context the claim references, if any.",
    )
    output_quote: Optional[str] = Field(
        default=None,
        description="The verbatim slice of the LLM output this claim was extracted from.",
    )
    category: ClaimCategory = Field(default=ClaimCategory.FACTUAL)


class ClaimInput(BaseModel):
    """User-supplied claim for /verify/claims — lets callers skip extraction."""

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = Field(
        default=None,
        description="Caller-chosen claim id. Auto-generated if omitted.",
    )
    text: str = Field(..., min_length=1)
    source_quote: Optional[str] = None
    category: ClaimCategory = Field(default=ClaimCategory.FACTUAL)


class VerifyClaimsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_context: str = Field(..., min_length=1)
    claims: list[ClaimInput] = Field(..., min_length=1)
    provider: Optional[str] = Field(default=None)
    model: Optional[str] = Field(default=None)


class VerifyQuickRequest(VerifyRequest):
    """Alias type for /verify/quick — same shape as VerifyRequest."""


class ClaimVerdict(BaseModel):
    claim_id: str
    grounding_score: int = Field(..., ge=0, le=100)
    grounding_level: GroundingLevel
    consistency_verdict: ConsistencyVerdict
    is_hallucination: bool = False
    status: ClaimStatus
    integrity_score: int = Field(..., ge=0, le=100)
    matched_passage: Optional[str] = None
    reasoning: str = ""


class ReportMetadata(BaseModel):
    provider: str = "anthropic"
    model: str
    request_id: str = Field(default_factory=lambda: _new_id("req"))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    duration_ms: int = 0
    extractor_ms: int = 0
    grounder_ms: int = 0
    consistency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    claim_count: int = 0


class IntegrityReport(BaseModel):
    overall_score: int = Field(..., ge=0, le=100)
    verified: list[ClaimVerdict] = Field(default_factory=list)
    uncertain: list[ClaimVerdict] = Field(default_factory=list)
    flagged: list[ClaimVerdict] = Field(default_factory=list)
    hallucinations: list[ClaimVerdict] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    metadata: ReportMetadata


class VerifyBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_context: str = Field(..., min_length=1)
    llm_output: str = Field(..., min_length=1)


class VerifyBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[VerifyBatchItem] = Field(..., min_length=1)
    mode: str = Field(
        default="full",
        pattern="^(full|quick)$",
        description="Verification mode per item: 'full' runs the consistency stage; 'quick' is grounding-only.",
    )
    provider: Optional[str] = Field(default=None)
    model: Optional[str] = Field(default=None)


class BatchItemError(BaseModel):
    code: str
    message: str


class BatchItemResult(BaseModel):
    index: int
    status: str = Field(..., pattern="^(ok|error)$")
    report: Optional[IntegrityReport] = None
    error: Optional[BatchItemError] = None


class BatchRollup(BaseModel):
    item_count: int
    ok_count: int
    error_count: int
    hallucination_item_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    duration_ms: int
    concurrency: int
    mode: str


class VerifyBatchReport(BaseModel):
    rollup: BatchRollup
    results: list[BatchItemResult]


class TraceClaimEvidence(BaseModel):
    """Per-claim evidence captured during verification.

    Bundles the grounding passage and location, the raw consistency reasoning,
    and any internal-contradiction links — i.e. the material the pipeline
    produced while computing the report but that `ClaimVerdict` flattens away.
    """

    claim_id: str
    text: str
    category: ClaimCategory
    output_quote: Optional[str] = None
    grounding_score: int
    grounding_level: GroundingLevel
    matched_passage: Optional[str] = None
    match_location: Optional[tuple[int, int]] = None
    grounding_reasoning: str = ""
    used_semantic_fallback: bool = False
    consistency_verdict: ConsistencyVerdict
    source_consistent: bool
    internal_consistent: bool
    confidence: int
    consistency_reasoning: str = ""
    contradicts: list[str] = Field(default_factory=list)


class VerifyTrace(BaseModel):
    """Explainability artifact for a single verify call.

    Keyed by `request_id`, returned by `GET /verify/trace/{request_id}`.
    """

    request_id: str
    endpoint: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    report: IntegrityReport
    evidence: list[TraceClaimEvidence] = Field(default_factory=list)


class AuditEvent(BaseModel):
    """A single audit record as returned by `GET /audit/events`."""

    request_id: str
    timestamp: str
    endpoint: str
    model: str
    status_code: int
    latency_ms: int
    overall_score: Optional[int] = None
    claim_count: int = 0
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    batch_id: Optional[str] = None
    batch_index: Optional[int] = None
    error: Optional[str] = None


class AuditEventsResponse(BaseModel):
    count: int
    events: list[AuditEvent]
