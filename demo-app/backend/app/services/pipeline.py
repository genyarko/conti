from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.models.schemas import (
    AnalyzeResponse,
    ContractSummary,
    ParsedContract,
    RiskLevel,
    VerificationStatus,
    VerifiedFinding,
)
from app.services.analyzer import ContractAnalyzer
from app.services.verifier import TrustLayerVerifier

log = logging.getLogger(__name__)


@dataclass
class AnalysisPipeline:
    analyzer: Optional[ContractAnalyzer] = None
    verifier: Optional[TrustLayerVerifier] = None

    async def run(
        self,
        contract: ParsedContract,
        *,
        skip_verification: bool = False,
    ) -> AnalyzeResponse:
        analyzer = self.analyzer or ContractAnalyzer()

        t0 = time.perf_counter()
        analysis = await analyzer.analyze(contract.clauses, filename=contract.filename)
        analyze_ms = int((time.perf_counter() - t0) * 1000)

        all_findings = analysis.findings + analysis.missing_clauses

        if skip_verification or not all_findings:
            verified_findings = [_as_unchecked(f) for f in analysis.findings]
            missing_unchecked = analysis.missing_clauses
            summary = _summary_without_verification(analysis.summary)
            return AnalyzeResponse(
                contract_id=contract.contract_id,
                filename=contract.filename,
                doc_type=contract.doc_type,
                summary=summary,
                clauses=contract.clauses,
                findings=verified_findings,
                removed_findings=[],
                missing_clauses=missing_unchecked,
                metadata={
                    "analyze_ms": analyze_ms,
                    "verify_ms": 0,
                    "verification_skipped": True,
                },
            )

        v0 = time.perf_counter()
        verifier = self.verifier
        if verifier is None:
            async with TrustLayerVerifier() as v:
                outcome = await v.verify_findings(all_findings, contract.clauses)
        else:
            outcome = await verifier.verify_findings(all_findings, contract.clauses)
        verify_ms = int((time.perf_counter() - v0) * 1000)

        # Split verified results back into clause-level vs missing-clause buckets.
        findings_bucket: list[VerifiedFinding] = []
        missing_bucket_unverified = []
        for vf in outcome.verified:
            if vf.finding.section_id == "missing":
                # Keep missing clauses that survived verification as raw Findings,
                # which matches how the frontend renders MissingClauseAlert.
                missing_bucket_unverified.append(vf.finding)
            else:
                findings_bucket.append(vf)

        summary = ContractSummary(
            contract_type=analysis.summary.contract_type,
            overall_risk=_compute_overall_risk(findings_bucket, analysis.summary.overall_risk),
            integrity_score=_compute_integrity_score(findings_bucket),
            plain_language_summary=analysis.summary.plain_language_summary,
            key_parties=analysis.summary.key_parties,
        )

        return AnalyzeResponse(
            contract_id=contract.contract_id,
            filename=contract.filename,
            doc_type=contract.doc_type,
            summary=summary,
            clauses=contract.clauses,
            findings=findings_bucket,
            removed_findings=outcome.removed,
            missing_clauses=missing_bucket_unverified,
            metadata={
                "analyze_ms": analyze_ms,
                "verify_ms": verify_ms,
                "num_findings": len(findings_bucket),
                "num_removed": len(outcome.removed),
            },
        )


def _as_unchecked(finding) -> VerifiedFinding:
    return VerifiedFinding(
        finding=finding,
        verification_status=VerificationStatus.UNCHECKED,
        integrity_score=0,
        grounding_score=0,
        reasoning="Verification was skipped for this run.",
    )


def _summary_without_verification(base: ContractSummary) -> ContractSummary:
    return ContractSummary(
        contract_type=base.contract_type,
        overall_risk=base.overall_risk,
        integrity_score=0,
        plain_language_summary=base.plain_language_summary,
        key_parties=base.key_parties,
    )


_RISK_ORDER = {
    RiskLevel.OK: 0,
    RiskLevel.INFO: 1,
    RiskLevel.WARNING: 2,
    RiskLevel.CRITICAL: 3,
}


def _compute_overall_risk(findings: list[VerifiedFinding], fallback: RiskLevel) -> RiskLevel:
    if not findings:
        return fallback
    worst = max(
        (vf.finding.risk for vf in findings),
        key=lambda r: _RISK_ORDER.get(r, 0),
    )
    return worst


def _compute_integrity_score(findings: list[VerifiedFinding]) -> int:
    if not findings:
        return 100
    total = sum(vf.integrity_score for vf in findings)
    return int(round(total / len(findings)))
