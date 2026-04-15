from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config import settings
from app.models.schemas import (
    Clause,
    Finding,
    VerificationStatus,
    VerifiedFinding,
)

log = logging.getLogger(__name__)

STATUS_MAP = {
    "verified": VerificationStatus.VERIFIED,
    "uncertain": VerificationStatus.UNCERTAIN,
    "flagged": VerificationStatus.FLAGGED,
    "hallucination": VerificationStatus.HALLUCINATION,
}


@dataclass
class VerificationOutcome:
    verified: list[VerifiedFinding]
    removed: list[VerifiedFinding]


class TrustLayerVerifier:
    """Verifies analyzer findings against clause text via the TrustLayer engine.

    For each finding, we construct a source_context (the clause text, or all
    clauses if the finding is about a missing clause) and two claims derived
    from the finding's summary + recommendation. TrustLayer returns an
    IntegrityReport per finding — we project that down to a single
    VerifiedFinding status.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        max_concurrency: Optional[int] = None,
    ) -> None:
        self._base_url = (base_url or settings.trustlayer_base_url).rstrip("/")
        self._timeout = timeout_seconds or settings.trustlayer_timeout_seconds
        self._http = http_client
        self._owns_client = http_client is None
        self._semaphore = asyncio.Semaphore(
            max_concurrency or settings.max_findings_verified_in_parallel
        )

    async def __aenter__(self) -> "TrustLayerVerifier":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._owns_client and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def verify_findings(
        self,
        findings: list[Finding],
        clauses: list[Clause],
    ) -> VerificationOutcome:
        if not findings:
            return VerificationOutcome(verified=[], removed=[])

        clause_index = {c.section_id: c for c in clauses}
        full_contract_text = "\n\n".join(
            f"[{c.section_id}] {c.title}\n{c.text}".strip() for c in clauses
        )

        tasks = [
            self._verify_one(f, clause_index, full_contract_text) for f in findings
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        verified: list[VerifiedFinding] = []
        removed: list[VerifiedFinding] = []
        for finding, result in zip(findings, results):
            if isinstance(result, Exception):
                log.warning("verifier: finding %s failed to verify: %s", finding.id, result)
                verified.append(_unchecked(finding, reason=str(result)))
                continue
            assert isinstance(result, VerifiedFinding)
            if result.removed:
                removed.append(result)
            else:
                verified.append(result)
        return VerificationOutcome(verified=verified, removed=removed)

    async def _verify_one(
        self,
        finding: Finding,
        clause_index: dict[str, Clause],
        full_contract_text: str,
    ) -> VerifiedFinding:
        async with self._semaphore:
            source_context = self._source_for(finding, clause_index, full_contract_text)
            claims = self._claims_for(finding)

            payload = {"source_context": source_context, "claims": claims}
            assert self._http is not None
            resp = await self._http.post(
                f"{self._base_url}/verify/claims",
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"TrustLayer returned {resp.status_code}: {resp.text[:200]}"
                )
            report = resp.json()
            return _project_report(finding, report)

    @staticmethod
    def _source_for(
        finding: Finding,
        clause_index: dict[str, Clause],
        full_contract_text: str,
    ) -> str:
        if finding.section_id == "missing":
            # Missing-clause findings are checked against the whole contract:
            # we want the verifier to confirm the clause really is absent.
            return full_contract_text
        clause = clause_index.get(finding.section_id)
        if clause is None:
            return full_contract_text
        return f"{clause.title}\n{clause.text}" if clause.title else clause.text

    @staticmethod
    def _claims_for(finding: Finding) -> list[dict[str, Any]]:
        claims = [
            {
                "id": f"{finding.id}-summary",
                "text": finding.summary,
                "category": "interpretive",
            }
        ]
        if finding.recommendation:
            claims.append(
                {
                    "id": f"{finding.id}-rec",
                    "text": finding.recommendation,
                    "category": "recommendation",
                }
            )
        return claims


def _project_report(finding: Finding, report: dict[str, Any]) -> VerifiedFinding:
    verified = report.get("verified") or []
    uncertain = report.get("uncertain") or []
    flagged = report.get("flagged") or []
    hallucinations = report.get("hallucinations") or []

    all_verdicts: list[dict[str, Any]] = [
        *verified,
        *uncertain,
        *flagged,
        *hallucinations,
    ]
    # Score the finding by its WORST constituent claim so a hallucinated
    # recommendation pulls the whole finding into the hallucinations bucket.
    status_order = [
        VerificationStatus.HALLUCINATION,
        VerificationStatus.FLAGGED,
        VerificationStatus.UNCERTAIN,
        VerificationStatus.VERIFIED,
    ]
    worst = VerificationStatus.VERIFIED
    integrity = 100
    grounding = 100
    reasoning_parts: list[str] = []

    for verdict in all_verdicts:
        raw_status = str(verdict.get("status") or "").lower()
        status = STATUS_MAP.get(raw_status, VerificationStatus.UNCERTAIN)
        if status_order.index(status) < status_order.index(worst):
            worst = status
        try:
            integrity = min(integrity, int(verdict.get("integrity_score", integrity)))
            grounding = min(grounding, int(verdict.get("grounding_score", grounding)))
        except (TypeError, ValueError):
            pass
        r = verdict.get("reasoning")
        if isinstance(r, str) and r:
            reasoning_parts.append(r)

    if not all_verdicts:
        worst = VerificationStatus.UNCHECKED
        integrity = report.get("overall_score") or 0
        grounding = 0
        reasoning_parts.append("TrustLayer extracted no claims for this finding.")

    removed = worst == VerificationStatus.HALLUCINATION
    return VerifiedFinding(
        finding=finding,
        verification_status=worst,
        integrity_score=int(integrity),
        grounding_score=int(grounding),
        reasoning=" ".join(reasoning_parts)[:2000],
        removed=removed,
    )


def _unchecked(finding: Finding, *, reason: str) -> VerifiedFinding:
    return VerifiedFinding(
        finding=finding,
        verification_status=VerificationStatus.UNCHECKED,
        integrity_score=0,
        grounding_score=0,
        reasoning=f"Verification failed: {reason}",
        removed=False,
    )
