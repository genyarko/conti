from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from engine.app.models.schemas import Claim, ConsistencyVerdict
from engine.app.pipeline.consistency import (
    ConsistencyChecker,
    ContradictionPair,
    _coerce_confidence,
    _coerce_verdict,
    _contradicts_index,
    _parse_consistency_response,
    _parse_contradictions_response,
)


@dataclass
class FakeClient:
    """Returns canned responses in order; records all calls."""

    responses: list[str]
    calls: list[dict] = field(default_factory=list)
    _idx: int = 0

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
    ) -> str:
        self.calls.append(
            {"system": system, "user": user, "model": model, "max_tokens": max_tokens}
        )
        if self._idx >= len(self.responses):
            raise AssertionError("FakeClient ran out of canned responses")
        out = self.responses[self._idx]
        self._idx += 1
        return out


def _cresp(verdict: str, confidence: int = 8, reasoning: str = "ok") -> str:
    return json.dumps(
        {"verdict": verdict, "confidence": confidence, "reasoning": reasoning}
    )


def _xresp(pairs: list[dict]) -> str:
    return json.dumps({"contradictions": pairs})


SOURCE = (
    "The agreement auto-renews for successive one-year terms unless either party "
    "provides written notice of non-renewal at least 30 days before the end of the "
    "current term. The vendor's liability under this agreement is capped at fees paid "
    "in the prior 12 months."
)


# ---------- pure helpers ----------


def test_coerce_verdict_accepts_canonical_labels():
    assert _coerce_verdict("consistent") == ConsistencyVerdict.CONSISTENT
    assert _coerce_verdict(" MINOR_CONCERN ") == ConsistencyVerdict.MINOR_CONCERN
    assert _coerce_verdict("inconsistent") == ConsistencyVerdict.INCONSISTENT
    assert _coerce_verdict("contradictory") == ConsistencyVerdict.CONTRADICTORY


def test_coerce_verdict_handles_aliases_and_junk():
    assert _coerce_verdict("contradiction") == ConsistencyVerdict.CONTRADICTORY
    assert _coerce_verdict("minor-concern") == ConsistencyVerdict.MINOR_CONCERN
    # Unknown strings default to the skeptical label.
    assert _coerce_verdict("???") == ConsistencyVerdict.INCONSISTENT
    assert _coerce_verdict(None) == ConsistencyVerdict.INCONSISTENT


def test_coerce_confidence_clamps_range():
    assert _coerce_confidence(0) == 1
    assert _coerce_confidence(1) == 1
    assert _coerce_confidence(10) == 10
    assert _coerce_confidence(99) == 10
    assert _coerce_confidence("oops") == 5
    assert _coerce_confidence(None) == 5


def test_parse_consistency_response_strips_fences():
    raw = "```json\n" + _cresp("consistent", 9, "paraphrase of source") + "\n```"
    verdict, confidence, reasoning = _parse_consistency_response(raw)
    assert verdict == ConsistencyVerdict.CONSISTENT
    assert confidence == 9
    assert reasoning == "paraphrase of source"


def test_parse_consistency_response_recovers_embedded_json():
    raw = "Here you go: " + _cresp("minor_concern", 7) + " done"
    verdict, confidence, _ = _parse_consistency_response(raw)
    assert verdict == ConsistencyVerdict.MINOR_CONCERN
    assert confidence == 7


def test_parse_consistency_response_raises_on_missing_key():
    with pytest.raises(ValueError):
        _parse_consistency_response('{"confidence": 7}')


def test_parse_consistency_response_raises_on_non_json():
    with pytest.raises(ValueError):
        _parse_consistency_response("totally not json")


def test_parse_contradictions_filters_unknown_ids():
    raw = _xresp(
        [
            {"claim_a": "c1", "claim_b": "c2", "reasoning": "opposing"},
            {"claim_a": "c1", "claim_b": "c999", "reasoning": "ghost claim"},
        ]
    )
    pairs = _parse_contradictions_response(raw, {"c1", "c2", "c3"})
    assert len(pairs) == 1
    assert pairs[0].claim_a == "c1"
    assert pairs[0].claim_b == "c2"


def test_parse_contradictions_dedupes_symmetric_pairs():
    raw = _xresp(
        [
            {"claim_a": "c1", "claim_b": "c2"},
            {"claim_a": "c2", "claim_b": "c1"},
        ]
    )
    pairs = _parse_contradictions_response(raw, {"c1", "c2"})
    assert len(pairs) == 1


def test_parse_contradictions_empty_list_ok():
    pairs = _parse_contradictions_response(_xresp([]), {"c1"})
    assert pairs == []


def test_parse_contradictions_rejects_self_pairs():
    raw = _xresp([{"claim_a": "c1", "claim_b": "c1"}])
    assert _parse_contradictions_response(raw, {"c1"}) == []


def test_contradicts_index_is_symmetric():
    pairs = [ContradictionPair("c1", "c2", "x"), ContradictionPair("c2", "c3", "y")]
    idx = _contradicts_index(pairs)
    assert idx["c1"] == {"c2"}
    assert idx["c2"] == {"c1", "c3"}
    assert idx["c3"] == {"c2"}


# ---------- checker behavior ----------


@pytest.mark.asyncio
async def test_sound_claim_passes_source_and_internal_checks():
    claim = Claim(
        id="c1",
        text="The agreement renews for one-year terms unless a party gives 30 days' notice.",
    )
    fake = FakeClient(
        responses=[
            _cresp("consistent", 9, "faithful paraphrase"),
            _xresp([]),
        ]
    )
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    results = await checker.check([claim], SOURCE)

    assert len(results) == 1
    r = results[0]
    assert r.verdict == ConsistencyVerdict.CONSISTENT
    assert r.source_consistent is True
    assert r.internal_consistent is True
    assert r.confidence == 9
    assert r.contradicts == []


@pytest.mark.asyncio
async def test_overreaching_claim_gets_flagged_inconsistent():
    claim = Claim(
        id="c1",
        text="The agreement can never be terminated by either party.",
    )
    fake = FakeClient(
        responses=[
            _cresp("inconsistent", 9, "overreaches — notice-based termination exists"),
            _xresp([]),
        ]
    )
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    (r,) = await checker.check([claim], SOURCE)

    assert r.verdict == ConsistencyVerdict.INCONSISTENT
    assert r.source_consistent is False
    assert r.internal_consistent is True


@pytest.mark.asyncio
async def test_contradicting_claims_are_linked_bidirectionally():
    c1 = Claim(id="c1", text="The agreement auto-renews annually.")
    c2 = Claim(id="c2", text="The agreement does not auto-renew.")
    fake = FakeClient(
        responses=[
            _cresp("consistent", 9),  # c1 source check
            _cresp("contradictory", 9),  # c2 source check
            _xresp([{"claim_a": "c1", "claim_b": "c2", "reasoning": "direct opposite"}]),
        ]
    )
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    results = await checker.check([c1, c2], SOURCE)
    by_id = {r.claim_id: r for r in results}

    # Both claims are internally inconsistent and reference each other.
    assert by_id["c1"].internal_consistent is False
    assert by_id["c2"].internal_consistent is False
    assert by_id["c1"].contradicts == ["c2"]
    assert by_id["c2"].contradicts == ["c1"]

    # c1 was source-consistent but got escalated due to the contradiction.
    assert by_id["c1"].verdict == ConsistencyVerdict.CONTRADICTORY
    assert by_id["c1"].source_consistent is False
    # c2 was already contradictory per source check.
    assert by_id["c2"].verdict == ConsistencyVerdict.CONTRADICTORY


@pytest.mark.asyncio
async def test_minor_concern_marks_source_consistent_true():
    claim = Claim(
        id="c1",
        text="The vendor's liability is capped at one year of fees.",
    )
    fake = FakeClient(
        responses=[
            _cresp("minor_concern", 6, "paraphrase drops 'prior 12 months' qualifier"),
            _xresp([]),
        ]
    )
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    (r,) = await checker.check([claim], SOURCE)
    assert r.verdict == ConsistencyVerdict.MINOR_CONCERN
    assert r.source_consistent is True
    assert r.internal_consistent is True


@pytest.mark.asyncio
async def test_empty_source_short_circuits_and_still_detects_contradictions():
    c1 = Claim(id="c1", text="The agreement auto-renews.")
    c2 = Claim(id="c2", text="The agreement does not auto-renew.")
    fake = FakeClient(
        responses=[
            _xresp([{"claim_a": "c1", "claim_b": "c2", "reasoning": "opposing"}]),
        ]
    )
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    results = await checker.check([c1, c2], "   ")

    # No per-claim source-consistency Claude calls were made.
    assert len(fake.calls) == 1
    for r in results:
        assert r.source_consistent is False
        assert r.verdict == ConsistencyVerdict.INCONSISTENT
    assert results[0].internal_consistent is False
    assert results[1].internal_consistent is False


@pytest.mark.asyncio
async def test_empty_claim_list_returns_empty_without_calls():
    fake = FakeClient(responses=[])
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    results = await checker.check([], SOURCE)
    assert results == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_single_claim_skips_contradiction_call():
    claim = Claim(id="c1", text="The agreement auto-renews annually.")
    fake = FakeClient(responses=[_cresp("consistent", 8)])
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    (r,) = await checker.check([claim], SOURCE)
    # Only the source-consistency call should fire.
    assert len(fake.calls) == 1
    assert r.internal_consistent is True


@pytest.mark.asyncio
async def test_malformed_consistency_response_raises():
    claim = Claim(id="c1", text="Anything.")
    fake = FakeClient(responses=["not json", _xresp([])])
    checker = ConsistencyChecker(client=fake, model="m", max_tokens=256)
    with pytest.raises(ValueError):
        await checker.check([claim], SOURCE)
