"""End-to-end pipeline test with Claude and TrustLayer stubbed out."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.models.schemas import RiskLevel, VerificationStatus
from app.services.analyzer import ContractAnalyzer
from app.services.ingest import ingest_text
from app.services.pipeline import AnalysisPipeline
from app.services.verifier import TrustLayerVerifier, VerificationOutcome, VerifiedFinding


SAMPLE_CONTRACT = """SAMPLE AGREEMENT

1. Fees
Provider may raise fees at any time without notice.

2. Liability
Provider's total liability is capped at $1.

3. Termination
Either party may terminate with 30 days' notice."""


ANALYZER_RESPONSE = {
    "contract_type": "Sample Agreement",
    "parties": ["Provider", "Customer"],
    "plain_language_summary": "A lopsided agreement with weak protections.",
    "overall_risk": "critical",
    "findings": [
        {
            "section_id": "1",
            "title": "Unilateral fee increase",
            "risk": "critical",
            "category": "payment",
            "summary": "Provider can raise fees at any time without notice.",
            "recommendation": "Require advance notice and cap annual increases.",
            "clause_quote": "Provider may raise fees at any time without notice.",
        },
        {
            "section_id": "2",
            "title": "Liability cap is too low",
            "risk": "critical",
            "category": "liability",
            "summary": "Liability is capped at $1, effectively zero.",
            "recommendation": "Raise cap to 12 months of fees paid.",
            "clause_quote": "Provider's total liability is capped at $1.",
        },
    ],
    "missing_clauses": [
        {
            "title": "Confidentiality",
            "risk": "warning",
            "category": "missing_clause",
            "summary": "No confidentiality provision is present.",
            "recommendation": "Add a mutual confidentiality clause.",
        }
    ],
}


@dataclass
class FakeClaudeClient:
    response: dict[str, Any]

    async def create_message(self, **kwargs: Any) -> str:
        raise AssertionError("analyzer should use create_with_tool, not create_message")

    async def create_with_tool(self, **kwargs: Any) -> dict[str, Any]:
        return self.response


class FakeVerifier:
    async def verify_findings(self, findings, clauses):  # noqa: D401 — test stub
        verified: list[VerifiedFinding] = []
        removed: list[VerifiedFinding] = []
        for f in findings:
            # Simulate: the second finding's recommendation is ruled a hallucination.
            if f.title == "Liability cap is too low":
                removed.append(
                    VerifiedFinding(
                        finding=f,
                        verification_status=VerificationStatus.HALLUCINATION,
                        integrity_score=5,
                        grounding_score=0,
                        reasoning="Fabricated recommendation.",
                        removed=True,
                    )
                )
            else:
                verified.append(
                    VerifiedFinding(
                        finding=f,
                        verification_status=VerificationStatus.VERIFIED,
                        integrity_score=92,
                        grounding_score=95,
                        reasoning="Supported by the clause.",
                        removed=False,
                    )
                )
        return VerificationOutcome(verified=verified, removed=removed)


@pytest.mark.asyncio
async def test_pipeline_separates_verified_and_removed():
    contract = ingest_text(SAMPLE_CONTRACT, filename="sample.txt")
    analyzer = ContractAnalyzer(
        client=FakeClaudeClient(response=ANALYZER_RESPONSE),
        model="test-model",
        max_tokens=1024,
    )
    pipeline = AnalysisPipeline(analyzer=analyzer, verifier=FakeVerifier())

    response = await pipeline.run(contract)

    assert response.summary.contract_type == "Sample Agreement"
    assert response.summary.overall_risk == RiskLevel.CRITICAL
    titles = [vf.finding.title for vf in response.findings]
    assert "Unilateral fee increase" in titles
    removed_titles = [vf.finding.title for vf in response.removed_findings]
    assert "Liability cap is too low" in removed_titles
    # Missing clause lives in its own bucket.
    missing_titles = [f.title for f in response.missing_clauses]
    assert "Confidentiality" in missing_titles


# Regression: the analyzer used to dump everything into missing_clauses when
# the schema made findings hard to populate, and the integrity score would
# return a hardcoded 100 — leaving the UI showing "100 Trusted" next to a
# Critical risk badge. The score must reflect missing-clause severity.
ANALYZER_RESPONSE_NO_CLAUSE_FINDINGS = {
    "contract_type": "Service Agreement",
    "parties": ["Provider", "Customer"],
    "plain_language_summary": "Provider-favorable contract.",
    "overall_risk": "critical",
    "findings": [],
    "missing_clauses": [
        {
            "title": "Limitation of Liability",
            "risk": "critical",
            "summary": "No liability cap.",
            "recommendation": "Add a mutual cap.",
        },
        {
            "title": "Service Level Agreement",
            "risk": "warning",
            "summary": "No SLA defined.",
            "recommendation": "Define service levels.",
        },
    ],
}


@pytest.mark.asyncio
async def test_score_reflects_missing_clauses_when_no_findings():
    contract = ingest_text(SAMPLE_CONTRACT, filename="sample.txt")
    analyzer = ContractAnalyzer(
        client=FakeClaudeClient(response=ANALYZER_RESPONSE_NO_CLAUSE_FINDINGS),
        model="test-model",
        max_tokens=1024,
    )
    pipeline = AnalysisPipeline(analyzer=analyzer, verifier=FakeVerifier())

    response = await pipeline.run(contract)

    assert len(response.findings) == 0
    assert len(response.missing_clauses) == 2
    # Critical missing clause must drag the score below 100.
    assert response.summary.integrity_score < 100
    assert response.summary.integrity_score <= 50
    # Risk badge must reflect missing-clause severity, not just findings.
    assert response.summary.overall_risk == RiskLevel.CRITICAL


@pytest.mark.asyncio
async def test_score_is_100_only_when_truly_clean():
    contract = ingest_text(SAMPLE_CONTRACT, filename="sample.txt")
    clean_response = {
        "contract_type": "Service Agreement",
        "parties": ["Provider", "Customer"],
        "plain_language_summary": "Clean contract.",
        "overall_risk": "ok",
        "findings": [],
        "missing_clauses": [],
    }
    analyzer = ContractAnalyzer(
        client=FakeClaudeClient(response=clean_response),
        model="test-model",
        max_tokens=1024,
    )
    pipeline = AnalysisPipeline(analyzer=analyzer, verifier=FakeVerifier())

    response = await pipeline.run(contract)

    assert response.summary.integrity_score == 100
    assert response.summary.overall_risk == RiskLevel.OK


@pytest.mark.asyncio
async def test_findings_with_optional_clause_quote():
    """clause_quote is no longer required — verify finding survives without it."""
    response_no_quotes = {
        "contract_type": "Sample Agreement",
        "parties": ["Provider", "Customer"],
        "plain_language_summary": "Test.",
        "overall_risk": "warning",
        "findings": [
            {
                "section_id": "1",
                "title": "Missing-quote finding",
                "risk": "warning",
                "category": "payment",
                "summary": "Issue without a clause_quote field.",
                "recommendation": "Fix it.",
                # clause_quote intentionally omitted
            }
        ],
        "missing_clauses": [],
    }
    contract = ingest_text(SAMPLE_CONTRACT, filename="sample.txt")
    analyzer = ContractAnalyzer(
        client=FakeClaudeClient(response=response_no_quotes),
        model="test-model",
        max_tokens=1024,
    )
    pipeline = AnalysisPipeline(analyzer=analyzer, verifier=FakeVerifier())

    response = await pipeline.run(contract)
    titles = [vf.finding.title for vf in response.findings]
    assert "Missing-quote finding" in titles
    finding = next(vf.finding for vf in response.findings if vf.finding.title == "Missing-quote finding")
    assert finding.clause_quote is None
