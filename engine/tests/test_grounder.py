from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from engine.app.models.schemas import Claim, GroundingLevel
from engine.app.pipeline.grounder import (
    ClaimGrounder,
    _best_match,
    _iter_passages,
    _level_for,
    _parse_grounder_response,
    _semantic_score,
)


@dataclass
class FakeClient:
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


def _gresp(support: str, matched_passage, confidence: int = 80, reasoning: str = "ok") -> str:
    return json.dumps(
        {
            "support": support,
            "matched_passage": matched_passage,
            "confidence": confidence,
            "reasoning": reasoning,
        }
    )


SOURCE = (
    "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. "
    "It is named after the engineer Gustave Eiffel, whose company designed and built the tower. "
    "Constructed from 1887 to 1889, it was the tallest man-made structure in the world for 41 years."
)


# ---------- pure helpers ----------


def test_iter_passages_splits_sentences():
    passages = _iter_passages(SOURCE)
    assert len(passages) == 3
    assert passages[0][2].startswith("The Eiffel Tower")
    for start, end, text in passages:
        assert SOURCE[start:end] == text


def test_iter_passages_empty_source():
    assert _iter_passages("") == []


def test_iter_passages_single_blob():
    # No sentence terminator — whole blob becomes one passage.
    passages = _iter_passages("just some text with no period")
    assert len(passages) == 1
    assert passages[0][2] == "just some text with no period"


def test_best_match_finds_direct_sentence():
    claim = "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France."
    match = _best_match(claim, SOURCE)
    assert match.score >= 95
    assert "Champ de Mars" in match.passage
    assert SOURCE[match.start : match.end] == match.passage


def test_best_match_fabricated_claim_is_not_verified():
    claim = "The Eiffel Tower was demolished in 1923 during a fire."
    match = _best_match(claim, SOURCE)
    assert match.score < 90


def test_best_match_empty_source_returns_zero():
    match = _best_match("anything", "")
    assert match.score == 0
    assert match.passage == ""


def test_level_for_thresholds():
    assert _level_for(95) == GroundingLevel.GROUNDED
    assert _level_for(90) == GroundingLevel.GROUNDED
    assert _level_for(89) == GroundingLevel.PARTIALLY_GROUNDED
    assert _level_for(70) == GroundingLevel.PARTIALLY_GROUNDED
    assert _level_for(69) == GroundingLevel.UNGROUNDED
    assert _level_for(0) == GroundingLevel.UNGROUNDED


def test_semantic_score_full_is_high():
    assert _semantic_score("full", 90) >= 90
    assert _semantic_score("full", 0) >= 90


def test_semantic_score_partial_is_mid():
    assert 70 <= _semantic_score("partial", 80) <= 89
    assert 70 <= _semantic_score("partial", 0) <= 89


def test_semantic_score_none_is_low():
    assert _semantic_score("none", 90) < 50
    assert _semantic_score("none", 0) < 50


def test_semantic_score_handles_bad_confidence():
    assert 70 <= _semantic_score("partial", "oops") <= 89


def test_parse_grounder_strips_code_fences():
    raw = "```json\n" + _gresp("full", "Paris", 80) + "\n```"
    data = _parse_grounder_response(raw)
    assert data["support"] == "full"


def test_parse_grounder_recovers_embedded_json():
    raw = "Here: " + _gresp("none", None, 90) + " done"
    data = _parse_grounder_response(raw)
    assert data["support"] == "none"


def test_parse_grounder_raises_on_missing_key():
    with pytest.raises(ValueError):
        _parse_grounder_response('{"foo": 1}')


def test_parse_grounder_raises_on_non_json():
    with pytest.raises(ValueError):
        _parse_grounder_response("not json at all")


# ---------- grounder behavior ----------


@pytest.mark.asyncio
async def test_grounded_claim_skips_llm_call():
    claim = Claim(
        text="The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France."
    )
    fake = FakeClient(responses=[])
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    result = await grounder.ground(claim, SOURCE)

    assert result.grounding_level == GroundingLevel.GROUNDED
    assert result.grounding_score >= 90
    assert result.used_semantic_fallback is False
    assert result.matched_passage is not None
    assert result.match_location is not None
    start, end = result.match_location
    assert SOURCE[start:end] == result.matched_passage
    assert fake.calls == []


@pytest.mark.asyncio
async def test_paraphrased_claim_triggers_semantic_fallback():
    # Paraphrase with low lexical overlap — fuzzy should miss, Claude rescues it.
    claim = Claim(
        text="Gustave Eiffel's engineering firm was responsible for building the tower."
    )
    supporting_passage = (
        "It is named after the engineer Gustave Eiffel, whose company designed and built the tower"
    )
    fake = FakeClient(responses=[_gresp("full", supporting_passage, 85)])
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    result = await grounder.ground(claim, SOURCE)

    assert result.used_semantic_fallback is True
    assert result.grounding_level == GroundingLevel.GROUNDED
    assert result.grounding_score >= 90
    assert result.matched_passage == supporting_passage
    assert result.match_location is not None
    start, end = result.match_location
    assert SOURCE[start:end] == supporting_passage
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_partially_supported_claim_is_mid_range():
    claim = Claim(text="The Eiffel Tower weighs approximately 10,100 tonnes.")
    fake = FakeClient(
        responses=[_gresp("partial", "wrought-iron lattice tower", 75)]
    )
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    result = await grounder.ground(claim, SOURCE)

    assert result.used_semantic_fallback is True
    assert 70 <= result.grounding_score <= 89
    assert result.grounding_level == GroundingLevel.PARTIALLY_GROUNDED
    assert result.matched_passage == "wrought-iron lattice tower"


@pytest.mark.asyncio
async def test_fabricated_claim_scores_low():
    claim = Claim(text="The Eiffel Tower was demolished in 1923 during a fire.")
    fake = FakeClient(responses=[_gresp("none", None, 95)])
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    result = await grounder.ground(claim, SOURCE)

    assert result.used_semantic_fallback is True
    assert result.grounding_level == GroundingLevel.UNGROUNDED
    assert result.grounding_score < 50
    assert result.matched_passage is None
    assert result.match_location is None


@pytest.mark.asyncio
async def test_empty_source_short_circuits_without_llm():
    claim = Claim(text="Some unrelated claim.")
    fake = FakeClient(responses=[])
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    result = await grounder.ground(claim, "   ")

    assert result.grounding_level == GroundingLevel.UNGROUNDED
    assert result.grounding_score == 0
    assert result.used_semantic_fallback is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_hallucinated_claude_passage_falls_back_to_fuzzy_passage():
    # Claim has enough lexical overlap to hit partial fuzzy (>=70) but not verified (>=90),
    # and Claude returns a passage that does not appear verbatim in source.
    claim = Claim(
        text="The Eiffel Tower is an iron lattice tower located on the Champ de Mars in Paris."
    )
    fake = FakeClient(
        responses=[_gresp("full", "totally fabricated passage not in source", 90)]
    )
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    result = await grounder.ground(claim, SOURCE)

    assert result.used_semantic_fallback is True
    # Passage must be verbatim from source — fall back to fuzzy passage.
    assert result.matched_passage is not None
    assert result.matched_passage in SOURCE


@pytest.mark.asyncio
async def test_ground_many_runs_all_claims_concurrently():
    claims = [
        Claim(
            text="The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France."
        ),
        Claim(text="The tower fires lasers at aircraft every night."),
    ]
    # Only the second claim needs an LLM call (first is a direct match).
    fake = FakeClient(responses=[_gresp("none", None, 92)])
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    results = await grounder.ground_many(claims, SOURCE)

    assert len(results) == 2
    assert results[0].grounding_level == GroundingLevel.GROUNDED
    assert results[0].used_semantic_fallback is False
    assert results[1].grounding_level == GroundingLevel.UNGROUNDED
    assert results[1].used_semantic_fallback is True
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_ground_many_empty_list_makes_no_calls():
    fake = FakeClient(responses=[])
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    results = await grounder.ground_many([], SOURCE)
    assert results == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_malformed_semantic_response_raises():
    claim = Claim(text="Totally unrelated claim about quantum chromodynamics.")
    fake = FakeClient(responses=["not json at all"])
    grounder = ClaimGrounder(client=fake, model="m", max_tokens=256)
    with pytest.raises(ValueError):
        await grounder.ground(claim, SOURCE)
