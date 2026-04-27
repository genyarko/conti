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
        provider: Optional[str] = None,
        model: Optional[str] = None,
        image_parts: Optional[list[tuple[bytes, str]]] = None,
    ) -> AnalyzeResponse:
        analyzer = self.analyzer or ContractAnalyzer(
            provider=provider, model=model
        )

        t0 = time.perf_counter()
        analysis = await analyzer.analyze(
            contract.clauses,
            filename=contract.filename,
            image_parts=image_parts,
        )
        analyze_ms = int((time.perf_counter() - t0) * 1000)
        used_multimodal = bool(image_parts) and analyzer.supports_multimodal
        analyzer_meta = {
            "provider": getattr(analyzer, "_provider", None),
            "model": getattr(analyzer, "_model", None),
            "multimodal": used_multimodal,
            "image_parts_count": len(image_parts) if image_parts else 0,
        }

        all_findings = analysis.findings + analysis.missing_clauses

        if skip_verification or not all_findings:
            verified_findings = [_as_unchecked(f) for f in analysis.findings]
            missing_unchecked = analysis.missing_clauses
            summary = _summary_without_verification(
                analysis.summary,
                missing_clauses=missing_unchecked,
                skip_verification=skip_verification,
            )
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
                    "analyzer": {**analysis.metadata, **analyzer_meta},
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
            overall_risk=_compute_overall_risk(
                findings_bucket,
                missing_bucket_unverified,
                analysis.summary.overall_risk,
            ),
            integrity_score=_compute_integrity_score(
                findings_bucket,
                missing_bucket_unverified,
            ),
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
                "num_missing_clauses": len(missing_bucket_unverified),
                "analyzer": {**analysis.metadata, **analyzer_meta},
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


def _summary_without_verification(
    base: ContractSummary,
    *,
    missing_clauses: list,
    skip_verification: bool,
) -> ContractSummary:
    # When the user explicitly opts out of verification, score 0 reflects
    # "we did not check." When we just had nothing to verify (clean contract),
    # the score should reflect that — using the same formula as the verified
    # path, with an empty findings list.
    if skip_verification:
        score = 0
    else:
        score = _compute_integrity_score([], missing_clauses)
    return ContractSummary(
        contract_type=base.contract_type,
        overall_risk=_compute_overall_risk([], missing_clauses, base.overall_risk),
        integrity_score=score,
        plain_language_summary=base.plain_language_summary,
        key_parties=base.key_parties,
    )


_RISK_ORDER = {
    RiskLevel.OK: 0,
    RiskLevel.INFO: 1,
    RiskLevel.WARNING: 2,
    RiskLevel.CRITICAL: 3,
}


def _compute_overall_risk(
    findings: list[VerifiedFinding],
    missing_clauses: list,
    fallback: RiskLevel,
) -> RiskLevel:
    risks: list[RiskLevel] = [vf.finding.risk for vf in findings]
    risks.extend(m.risk for m in missing_clauses)
    if not risks:
        return fallback
    return max(risks, key=lambda r: _RISK_ORDER.get(r, 0))


def _compute_integrity_score(
    findings: list[VerifiedFinding],
    missing_clauses: list,
) -> int:
    """Single user-visible "trust this analysis" number.

    The score reflects how well the analyzer's output stood up to verification,
    penalized by the severity of clauses the analyzer flagged as absent. A
    perfect 100 is reserved for cases where the analyzer found no issues at
    all — clause-level OR missing — so the headline never contradicts a
    "Critical" risk badge sitting next to it.
    """
    critical_missing = sum(
        1 for m in missing_clauses if m.risk == RiskLevel.CRITICAL
    )
    warning_missing = sum(
        1 for m in missing_clauses if m.risk == RiskLevel.WARNING
    )

    if findings:
        avg = sum(vf.integrity_score for vf in findings) / len(findings)
        base = int(round(avg))
    elif missing_clauses:
        # No clause-level findings to verify, but the analyzer flagged
        # absent standard clauses. Cap below 100 — a "100 Trusted" badge
        # next to a list of critical missing clauses is a confidence lie.
        base = 50 if critical_missing else 75
    else:
        base = 100

    penalty = critical_missing * 10 + warning_missing * 3
    return max(0, min(100, base - penalty))
