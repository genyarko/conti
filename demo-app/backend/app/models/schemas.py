from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    OK = "ok"


class FindingCategory(str, Enum):
    LIABILITY = "liability"
    TERMINATION = "termination"
    PAYMENT = "payment"
    IP = "ip"
    CONFIDENTIALITY = "confidentiality"
    DATA_PRIVACY = "data_privacy"
    DISPUTE = "dispute"
    RENEWAL = "renewal"
    INDEMNITY = "indemnity"
    COMPLIANCE = "compliance"
    MISSING_CLAUSE = "missing_clause"
    OTHER = "other"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    UNCERTAIN = "uncertain"
    FLAGGED = "flagged"
    HALLUCINATION = "hallucination"
    UNCHECKED = "unchecked"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Clause(BaseModel):
    section_id: str = Field(..., description="Stable id within the contract (e.g. '1', '2.3', 'sec-4').")
    title: str = Field(default="", description="Heading / section title, if detected.")
    text: str = Field(..., description="Raw clause text.")
    start_char: int = Field(default=0, description="Offset into the parsed document.")
    end_char: int = Field(default=0)


class ParsedContract(BaseModel):
    contract_id: str = Field(default_factory=lambda: _new_id("ctr"))
    filename: str = ""
    doc_type: str = Field(default="unknown", description="pdf | docx | txt")
    raw_text: str
    clauses: list[Clause]
    metadata: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("fnd"))
    section_id: str = Field(..., description="Clause this finding refers to (or 'missing' for absent clauses).")
    title: str
    risk: RiskLevel
    category: FindingCategory = FindingCategory.OTHER
    summary: str = Field(..., description="Plain-language explanation of the issue.")
    recommendation: str = ""
    clause_quote: Optional[str] = Field(
        default=None,
        description="Verbatim slice of the clause the finding is grounded in.",
    )


class VerifiedFinding(BaseModel):
    finding: Finding
    verification_status: VerificationStatus
    integrity_score: int = Field(..., ge=0, le=100)
    grounding_score: int = Field(..., ge=0, le=100)
    reasoning: str = ""
    removed: bool = Field(
        default=False,
        description="True if integrity layer flagged this as a hallucination and suppressed it.",
    )


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: Optional[str] = None
    text: Optional[str] = Field(
        default=None,
        description="Raw contract text. Supply this if you did not use /upload.",
    )
    filename: Optional[str] = None
    skip_verification: bool = Field(
        default=False,
        description="If true, return analyzer findings without calling TrustLayer.",
    )


class ContractSummary(BaseModel):
    contract_type: str = "Unknown"
    overall_risk: RiskLevel = RiskLevel.INFO
    integrity_score: int = Field(default=0, ge=0, le=100)
    plain_language_summary: str = ""
    key_parties: list[str] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    contract_id: str
    filename: str = ""
    doc_type: str = "unknown"
    summary: ContractSummary
    clauses: list[Clause]
    findings: list[VerifiedFinding]
    removed_findings: list[VerifiedFinding] = Field(default_factory=list)
    missing_clauses: list[Finding] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class UploadResponse(BaseModel):
    contract_id: str
    filename: str
    doc_type: str
    num_clauses: int
    char_count: int
    clauses: list[Clause]
    raw_text: str
