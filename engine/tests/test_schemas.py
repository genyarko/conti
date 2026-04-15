import pytest
from pydantic import ValidationError

from engine.app.models.schemas import (
    Claim,
    ClaimCategory,
    ClaimStatus,
    ClaimVerdict,
    ConsistencyVerdict,
    GroundingLevel,
    IntegrityReport,
    ReportMetadata,
    VerifyRequest,
)


def test_verify_request_requires_fields():
    with pytest.raises(ValidationError):
        VerifyRequest(source_context="", llm_output="x")
    with pytest.raises(ValidationError):
        VerifyRequest(source_context="x", llm_output="")


def test_verify_request_accepts_optional_schema():
    req = VerifyRequest(
        source_context="The sky is blue.",
        llm_output="The sky is blue.",
        output_schema={"type": "object"},
    )
    assert req.output_schema == {"type": "object"}


def test_claim_defaults():
    c = Claim(text="A claim.")
    assert c.id.startswith("clm_")
    assert c.category == ClaimCategory.FACTUAL
    assert c.source_quote is None


def test_claim_verdict_score_range():
    with pytest.raises(ValidationError):
        ClaimVerdict(
            claim_id="clm_1",
            grounding_score=150,
            grounding_level=GroundingLevel.GROUNDED,
            consistency_verdict=ConsistencyVerdict.CONSISTENT,
            status=ClaimStatus.VERIFIED,
            integrity_score=50,
        )


def test_integrity_report_round_trip():
    report = IntegrityReport(
        overall_score=88,
        metadata=ReportMetadata(model="claude-opus-4-6", claim_count=0),
    )
    dumped = report.model_dump()
    rebuilt = IntegrityReport.model_validate(dumped)
    assert rebuilt.overall_score == 88
    assert rebuilt.metadata.model == "claude-opus-4-6"
