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
    claim_count: int = 0


class IntegrityReport(BaseModel):
    overall_score: int = Field(..., ge=0, le=100)
    verified: list[ClaimVerdict] = Field(default_factory=list)
    uncertain: list[ClaimVerdict] = Field(default_factory=list)
    flagged: list[ClaimVerdict] = Field(default_factory=list)
    hallucinations: list[ClaimVerdict] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    metadata: ReportMetadata
