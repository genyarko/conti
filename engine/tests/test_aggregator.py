from __future__ import annotations

from engine.app.models.schemas import (
    Claim,
    ClaimCategory,
    ClaimStatus,
    ConsistencyVerdict,
    GroundingLevel,
    ReportMetadata,
)
from engine.app.pipeline.aggregator import (
    ReportAggregator,
    _classify,
    _compute_integrity,
    _consistency_score,
    _overall_score,
    _type_modifier,
    aggregate_report,
)
from engine.app.pipeline.consistency import ConsistencyResult
from engine.app.pipeline.grounder import GroundingResult


def _claim(cid: str, cat: ClaimCategory = ClaimCategory.FACTUAL) -> Claim:
    return Claim(id=cid, text=f"claim {cid}", category=cat)


def _grounding(
    cid: str,
    score: int,
    level: GroundingLevel,
    *,
    passage: str | None = "p",
) -> GroundingResult:
    return GroundingResult(
        claim_id=cid,
        grounding_score=score,
        grounding_level=level,
        matched_passage=passage,
        match_location=(0, 1) if passage else None,
        reasoning="ground reason",
    )


def _consistency(
    cid: str,
    verdict: ConsistencyVerdict,
    *,
    confidence: int = 8,
    source_ok: bool | None = None,
    internal_ok: bool = True,
    contradicts: list[str] | None = None,
) -> ConsistencyResult:
    if source_ok is None:
        source_ok = verdict in (
            ConsistencyVerdict.CONSISTENT,
            ConsistencyVerdict.MINOR_CONCERN,
        )
    return ConsistencyResult(
        claim_id=cid,
        verdict=verdict,
        source_consistent=source_ok,
        internal_consistent=internal_ok,
        confidence=confidence,
        reasoning="cons reason",
        contradicts=contradicts or [],
    )


# ---------- pure helpers ----------


def test_consistency_score_maps_verdicts():
    assert _consistency_score(_consistency("c", ConsistencyVerdict.CONSISTENT)) == 100
    assert _consistency_score(_consistency("c", ConsistencyVerdict.MINOR_CONCERN)) == 75


def test_consistency_score_low_confidence_softens_negative_verdict():
    high = _consistency_score(
        _consistency("c", ConsistencyVerdict.INCONSISTENT, confidence=10)
    )
    low = _consistency_score(
        _consistency("c", ConsistencyVerdict.INCONSISTENT, confidence=1)
    )
    assert high < low  # low-confidence bad verdicts get lifted


def test_type_modifier_values():
    assert _type_modifier(ClaimCategory.FACTUAL) == 100
    assert _type_modifier(ClaimCategory.QUANTITATIVE) == 100
    assert _type_modifier(ClaimCategory.INTERPRETIVE) == 85
    assert _type_modifier(ClaimCategory.RECOMMENDATION) == 80


def test_compute_integrity_weighted_average():
    # 0.5*100 + 0.35*100 + 0.15*100 = 100
    assert _compute_integrity(100, 100, 100) == 100
    # 0.5*90 + 0.35*75 + 0.15*100 = 86.25 → 86
    assert _compute_integrity(90, 75, 100) == 86
    # All zeros → 0
    assert _compute_integrity(0, 0, 0) == 0


def test_compute_integrity_clamps_to_range():
    assert 0 <= _compute_integrity(0, 0, 0) <= 100
    assert _compute_integrity(200, 200, 200) == 100


# ---------- classification ----------


def test_classify_verified_requires_high_grounding_and_consistency():
    status, halluc = _classify(
        95, GroundingLevel.GROUNDED, _consistency("c", ConsistencyVerdict.CONSISTENT)
    )
    assert status == ClaimStatus.VERIFIED
    assert halluc is False


def test_classify_uncertain_for_mid_grounding():
    status, halluc = _classify(
        80,
        GroundingLevel.PARTIALLY_GROUNDED,
        _consistency("c", ConsistencyVerdict.CONSISTENT),
    )
    assert status == ClaimStatus.UNCERTAIN
    assert halluc is False


def test_classify_uncertain_for_minor_concern_on_strong_grounding():
    status, _ = _classify(
        95,
        GroundingLevel.GROUNDED,
        _consistency("c", ConsistencyVerdict.MINOR_CONCERN),
    )
    assert status == ClaimStatus.UNCERTAIN


def test_classify_flagged_for_low_grounding_with_source_consistent():
    status, halluc = _classify(
        60,
        GroundingLevel.UNGROUNDED,
        _consistency("c", ConsistencyVerdict.CONSISTENT),
    )
    assert status == ClaimStatus.FLAGGED
    assert halluc is False


def test_classify_flagged_for_contradictory_even_with_decent_grounding():
    status, halluc = _classify(
        85,
        GroundingLevel.PARTIALLY_GROUNDED,
        _consistency("c", ConsistencyVerdict.CONTRADICTORY),
    )
    assert status == ClaimStatus.FLAGGED
    assert halluc is False


def test_classify_hallucination_needs_low_grounding_and_bad_consistency():
    status, halluc = _classify(
        30,
        GroundingLevel.UNGROUNDED,
        _consistency("c", ConsistencyVerdict.INCONSISTENT, source_ok=False),
    )
    assert status == ClaimStatus.HALLUCINATION
    assert halluc is True


def test_classify_low_grounding_but_consistent_is_only_flagged_not_halluc():
    # Grounding below 50 alone is not enough if the claim is source-consistent.
    status, halluc = _classify(
        40,
        GroundingLevel.UNGROUNDED,
        _consistency("c", ConsistencyVerdict.CONSISTENT),
    )
    assert status == ClaimStatus.FLAGGED
    assert halluc is False


# ---------- overall score ----------


def test_overall_score_no_claims_returns_perfect():
    assert _overall_score([], 0) == 100


def test_overall_score_penalizes_hallucinations():
    # Build three synthetic verdicts with integrity 100 each.
    claims = [_claim(f"c{i}") for i in range(3)]
    aggregator = ReportAggregator()
    report = aggregator.aggregate(
        claims=claims,
        groundings=[_grounding(c.id, 100, GroundingLevel.GROUNDED) for c in claims],
        consistencies=[
            _consistency(c.id, ConsistencyVerdict.CONSISTENT) for c in claims
        ],
        metadata=ReportMetadata(model="m"),
    )
    clean_score = report.overall_score

    # One hallucination should drop the overall score materially.
    claims2 = [_claim(f"c{i}") for i in range(3)]
    report2 = aggregator.aggregate(
        claims=claims2,
        groundings=[
            _grounding(claims2[0].id, 100, GroundingLevel.GROUNDED),
            _grounding(claims2[1].id, 100, GroundingLevel.GROUNDED),
            _grounding(claims2[2].id, 20, GroundingLevel.UNGROUNDED, passage=None),
        ],
        consistencies=[
            _consistency(claims2[0].id, ConsistencyVerdict.CONSISTENT),
            _consistency(claims2[1].id, ConsistencyVerdict.CONSISTENT),
            _consistency(
                claims2[2].id,
                ConsistencyVerdict.INCONSISTENT,
                source_ok=False,
            ),
        ],
        metadata=ReportMetadata(model="m"),
    )
    assert report2.overall_score < clean_score
    assert len(report2.hallucinations) == 1


# ---------- full aggregation ----------


def test_aggregate_report_buckets_claims_by_status():
    claims = [
        _claim("v"),
        _claim("u"),
        _claim("f"),
        _claim("h"),
    ]
    groundings = [
        _grounding("v", 95, GroundingLevel.GROUNDED),
        _grounding("u", 80, GroundingLevel.PARTIALLY_GROUNDED),
        _grounding("f", 60, GroundingLevel.UNGROUNDED, passage=None),
        _grounding("h", 20, GroundingLevel.UNGROUNDED, passage=None),
    ]
    consistencies = [
        _consistency("v", ConsistencyVerdict.CONSISTENT),
        _consistency("u", ConsistencyVerdict.CONSISTENT),
        _consistency("f", ConsistencyVerdict.CONSISTENT),
        _consistency("h", ConsistencyVerdict.CONTRADICTORY, source_ok=False),
    ]

    report = aggregate_report(claims, groundings, consistencies)

    ids = lambda bucket: [v.claim_id for v in bucket]
    assert ids(report.verified) == ["v"]
    assert ids(report.uncertain) == ["u"]
    assert ids(report.flagged) == ["f"]
    assert ids(report.hallucinations) == ["h"]

    # Metadata was populated with claim count.
    assert report.metadata.claim_count == 4
    # Every bucket's verdicts round-trip their claim_ids back into the
    # corresponding claim list.
    all_verdict_ids = {
        v.claim_id
        for bucket in (
            report.verified,
            report.uncertain,
            report.flagged,
            report.hallucinations,
        )
        for v in bucket
    }
    assert all_verdict_ids == {c.id for c in report.claims}


def test_aggregate_report_skips_claims_without_pipeline_results():
    claims = [_claim("c1"), _claim("c2")]
    groundings = [_grounding("c1", 95, GroundingLevel.GROUNDED)]
    consistencies = [_consistency("c1", ConsistencyVerdict.CONSISTENT)]

    report = aggregate_report(claims, groundings, consistencies)

    assert report.metadata.claim_count == 1
    assert [c.id for c in report.claims] == ["c1"]
    assert len(report.verified) == 1


def test_aggregate_report_reasoning_includes_contradiction_ids():
    claims = [_claim("a"), _claim("b")]
    groundings = [
        _grounding("a", 90, GroundingLevel.GROUNDED),
        _grounding("b", 90, GroundingLevel.GROUNDED),
    ]
    consistencies = [
        _consistency(
            "a",
            ConsistencyVerdict.CONTRADICTORY,
            source_ok=False,
            internal_ok=False,
            contradicts=["b"],
        ),
        _consistency(
            "b",
            ConsistencyVerdict.CONTRADICTORY,
            source_ok=False,
            internal_ok=False,
            contradicts=["a"],
        ),
    ]
    report = aggregate_report(claims, groundings, consistencies)
    verdicts_by_id = {
        v.claim_id: v
        for bucket in (report.flagged, report.hallucinations, report.verified, report.uncertain)
        for v in bucket
    }
    assert "b" in verdicts_by_id["a"].reasoning
    assert "a" in verdicts_by_id["b"].reasoning


def test_aggregate_report_empty_claims_returns_perfect_score():
    report = aggregate_report([], [], [])
    assert report.overall_score == 100
    assert report.claims == []
    assert report.metadata.claim_count == 0
