"""End-to-end pipeline test with Claude and TrustLayer stubbed out."""
from __future__ import annotations

import json
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
    response: str

    async def create_message(self, **kwargs: Any) -> str:
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
        client=FakeClaudeClient(response=json.dumps(ANALYZER_RESPONSE)),
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
