from app.models.schemas import Finding, FindingCategory, RiskLevel, VerificationStatus
from app.services.verifier import _project_report


def _finding() -> Finding:
    return Finding(
        section_id="1",
        title="Unilateral fee increase",
        risk=RiskLevel.WARNING,
        category=FindingCategory.PAYMENT,
        summary="Provider can raise fees at any time with only 10 days' notice.",
        recommendation="Cap annual price increases and require 60 days' notice.",
        clause_quote="Provider may increase the subscription fees at any time",
    )


_BUCKETS = {
    "verified": "verified",
    "uncertain": "uncertain",
    "flagged": "flagged",
    "hallucination": "hallucinations",
}


def _report(*claim_dicts, overall_score=80) -> dict:
    report = {
        "overall_score": overall_score,
        "verified": [],
        "uncertain": [],
        "flagged": [],
        "hallucinations": [],
    }
    for c in claim_dicts:
        report[_BUCKETS[c["status"]]].append(c)
    return report


def test_projects_verified_when_all_good():
    report = _report(
        {"status": "verified", "integrity_score": 92, "grounding_score": 95, "reasoning": "Supported."},
        {"status": "verified", "integrity_score": 90, "grounding_score": 93, "reasoning": "Supported."},
    )
    vf = _project_report(_finding(), report)
    assert vf.verification_status == VerificationStatus.VERIFIED
    assert vf.removed is False
    assert vf.integrity_score == 90


def test_projects_hallucination_when_any_worst():
    report = _report(
        {"status": "verified", "integrity_score": 92, "grounding_score": 95, "reasoning": "Supported."},
        {"status": "hallucination", "integrity_score": 10, "grounding_score": 5, "reasoning": "Fabricated."},
    )
    vf = _project_report(_finding(), report)
    assert vf.verification_status == VerificationStatus.HALLUCINATION
    assert vf.removed is True
    assert vf.integrity_score == 10


def test_no_claims_extracted_marks_unchecked():
    report = _report(overall_score=0)
    vf = _project_report(_finding(), report)
    assert vf.verification_status == VerificationStatus.UNCHECKED
    assert vf.removed is False
