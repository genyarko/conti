from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from engine.app.models.schemas import (
    Claim,
    ClaimCategory,
    ClaimStatus,
    ClaimVerdict,
    ConsistencyVerdict,
    GroundingLevel,
    IntegrityReport,
    ReportMetadata,
)
from engine.app.pipeline.consistency import ConsistencyResult
from engine.app.pipeline.grounder import GroundingResult
from engine.config import settings

log = logging.getLogger(__name__)

GROUNDING_WEIGHT = 0.50
CONSISTENCY_WEIGHT = 0.35
TYPE_WEIGHT = 0.15

# Score (0-100) assigned to each consistency verdict.
_VERDICT_SCORES: dict[ConsistencyVerdict, int] = {
    ConsistencyVerdict.CONSISTENT: 100,
    ConsistencyVerdict.MINOR_CONCERN: 75,
    ConsistencyVerdict.INCONSISTENT: 25,
    ConsistencyVerdict.CONTRADICTORY: 0,
}

# Modifier (0-100) reflecting how strictly a claim of this type must be grounded.
# Factual / quantitative claims demand strong evidence; interpretive and
# recommendation claims are often extrapolations and get a small floor boost.
_TYPE_MODIFIERS: dict[ClaimCategory, int] = {
    ClaimCategory.FACTUAL: 100,
    ClaimCategory.QUANTITATIVE: 100,
    ClaimCategory.INTERPRETIVE: 85,
    ClaimCategory.RECOMMENDATION: 80,
}


@dataclass
class AggregationInput:
    claim: Claim
    grounding: GroundingResult
    consistency: ConsistencyResult


def _consistency_score(result: ConsistencyResult) -> int:
    base = _VERDICT_SCORES.get(result.verdict, 25)
    # Nudge toward the base using confidence (1-10). High confidence in an
    # inconsistent verdict should stick; low confidence softens the penalty.
    conf = max(1, min(10, result.confidence))
    if base >= 75:
        return base
    # For negative verdicts, raise slightly when the model is uncertain.
    lift = (10 - conf) * 2
    return min(base + lift, 60)


def _type_modifier(category: ClaimCategory) -> int:
    return _TYPE_MODIFIERS.get(category, 100)


def _compute_integrity(
    grounding: int, consistency: int, type_mod: int
) -> int:
    raw = (
        GROUNDING_WEIGHT * grounding
        + CONSISTENCY_WEIGHT * consistency
        + TYPE_WEIGHT * type_mod
    )
    return max(0, min(100, int(round(raw))))


def _classify(
    grounding_score: int,
    grounding_level: GroundingLevel,
    consistency: ConsistencyResult,
) -> tuple[ClaimStatus, bool]:
    consistent = consistency.verdict in (
        ConsistencyVerdict.CONSISTENT,
        ConsistencyVerdict.MINOR_CONCERN,
    )
    hallucinates = (
        grounding_score < settings.hallucination_grounding_max
        and not consistent
    )
    if hallucinates:
        return ClaimStatus.HALLUCINATION, True

    contradicts_source = consistency.verdict == ConsistencyVerdict.CONTRADICTORY
    if grounding_score < settings.grounding_threshold_partial or contradicts_source:
        return ClaimStatus.FLAGGED, False

    if (
        grounding_score >= settings.grounding_threshold_verified
        and consistency.verdict == ConsistencyVerdict.CONSISTENT
    ):
        return ClaimStatus.VERIFIED, False

    # Grounding 70-89, or top grounding paired with minor concern.
    return ClaimStatus.UNCERTAIN, False


def _build_reasoning(
    grounding: GroundingResult, consistency: ConsistencyResult
) -> str:
    parts: list[str] = []
    if grounding.reasoning:
        parts.append(f"Grounding: {grounding.reasoning}")
    if consistency.reasoning:
        parts.append(f"Consistency: {consistency.reasoning}")
    if consistency.contradicts:
        parts.append(
            "Contradicts claim(s): " + ", ".join(consistency.contradicts)
        )
    return " | ".join(parts)


def _overall_score(
    verdicts: list[ClaimVerdict], hallucination_count: int
) -> int:
    if not verdicts:
        return 100
    base = sum(v.integrity_score for v in verdicts) / len(verdicts)
    # Each hallucination chips 10 points off the overall, capped at 30.
    penalty = min(30, hallucination_count * 10)
    return max(0, min(100, int(round(base - penalty))))


class ReportAggregator:
    def aggregate(
        self,
        claims: list[Claim],
        groundings: list[GroundingResult],
        consistencies: list[ConsistencyResult],
        *,
        metadata: ReportMetadata,
    ) -> IntegrityReport:
        g_by_id = {g.claim_id: g for g in groundings}
        c_by_id = {c.claim_id: c for c in consistencies}

        verdicts: list[ClaimVerdict] = []
        kept_claims: list[Claim] = []
        halluc_count = 0

        for claim in claims:
            grounding = g_by_id.get(claim.id)
            consistency = c_by_id.get(claim.id)
            if grounding is None or consistency is None:
                log.warning(
                    "aggregator: missing pipeline result for claim %s", claim.id
                )
                continue

            cons_score = _consistency_score(consistency)
            type_mod = _type_modifier(claim.category)
            integrity = _compute_integrity(
                grounding.grounding_score, cons_score, type_mod
            )
            status, is_halluc = _classify(
                grounding.grounding_score,
                grounding.grounding_level,
                consistency,
            )
            if is_halluc:
                halluc_count += 1

            verdicts.append(
                ClaimVerdict(
                    claim_id=claim.id,
                    grounding_score=grounding.grounding_score,
                    grounding_level=grounding.grounding_level,
                    consistency_verdict=consistency.verdict,
                    is_hallucination=is_halluc,
                    status=status,
                    integrity_score=integrity,
                    matched_passage=grounding.matched_passage,
                    reasoning=_build_reasoning(grounding, consistency),
                )
            )
            kept_claims.append(claim)

        buckets: dict[ClaimStatus, list[ClaimVerdict]] = {
            ClaimStatus.VERIFIED: [],
            ClaimStatus.UNCERTAIN: [],
            ClaimStatus.FLAGGED: [],
            ClaimStatus.HALLUCINATION: [],
        }
        for v in verdicts:
            buckets[v.status].append(v)

        metadata.claim_count = len(kept_claims)
        overall = _overall_score(verdicts, halluc_count)

        return IntegrityReport(
            overall_score=overall,
            verified=buckets[ClaimStatus.VERIFIED],
            uncertain=buckets[ClaimStatus.UNCERTAIN],
            flagged=buckets[ClaimStatus.FLAGGED],
            hallucinations=buckets[ClaimStatus.HALLUCINATION],
            claims=kept_claims,
            metadata=metadata,
        )


def aggregate_report(
    claims: list[Claim],
    groundings: list[GroundingResult],
    consistencies: list[ConsistencyResult],
    *,
    metadata: Optional[ReportMetadata] = None,
) -> IntegrityReport:
    md = metadata or ReportMetadata(model=settings.anthropic_model)
    return ReportAggregator().aggregate(
        claims, groundings, consistencies, metadata=md
    )
