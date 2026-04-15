from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.app import main as main_module
from engine.app.main import app
from engine.app.models.schemas import (
    Claim,
    ClaimCategory,
    ConsistencyVerdict,
    GroundingLevel,
    VerifyRequest,
)
from engine.app.pipeline.consistency import ConsistencyResult
from engine.app.pipeline.extractor import ExtractionResult
from engine.app.pipeline.grounder import GroundingResult
from engine.app.pipeline.orchestrator import VerifyPipeline


client = TestClient(app)


@dataclass
class _StubExtractor:
    claims: list[Claim]

    async def extract(self, llm_output: str) -> ExtractionResult:
        return ExtractionResult(claims=self.claims, raw_responses=[])


@dataclass
class _StubGrounder:
    results: dict[str, GroundingResult]

    async def ground_many(
        self, claims: list[Claim], source_context: str
    ) -> list[GroundingResult]:
        return [self.results[c.id] for c in claims]


@dataclass
class _StubConsistency:
    results: dict[str, ConsistencyResult]

    async def check(
        self, claims: list[Claim], source_context: str
    ) -> list[ConsistencyResult]:
        return [self.results[c.id] for c in claims]


def _make_pipeline_factory(
    claims: list[Claim],
    groundings: dict[str, GroundingResult],
    consistencies: dict[str, ConsistencyResult],
):
    def _factory() -> VerifyPipeline:
        return VerifyPipeline(
            extractor=_StubExtractor(claims=claims),
            grounder=_StubGrounder(results=groundings),
            consistency=_StubConsistency(results=consistencies),
        )

    return _factory


def _payload() -> dict[str, Any]:
    return {
        "source_context": "The Eiffel Tower is located in Paris, France.",
        "llm_output": "The Eiffel Tower is in Paris.",
    }


def test_verify_known_good_output_scores_high(monkeypatch):
    claim = Claim(id="c1", text="Eiffel Tower is in Paris.", category=ClaimCategory.FACTUAL)
    groundings = {
        "c1": GroundingResult(
            claim_id="c1",
            grounding_score=96,
            grounding_level=GroundingLevel.GROUNDED,
            matched_passage="The Eiffel Tower is located in Paris, France.",
            match_location=(0, 45),
            reasoning="direct match",
        )
    }
    consistencies = {
        "c1": ConsistencyResult(
            claim_id="c1",
            verdict=ConsistencyVerdict.CONSISTENT,
            source_consistent=True,
            internal_consistent=True,
            confidence=10,
            reasoning="faithful",
        )
    }
    monkeypatch.setattr(
        main_module,
        "VerifyPipeline",
        _make_pipeline_factory([claim], groundings, consistencies),
    )

    r = client.post("/verify", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["overall_score"] >= 95
    assert len(body["verified"]) == 1
    assert body["hallucinations"] == []
    assert body["metadata"]["claim_count"] == 1


def test_verify_catches_planted_hallucination(monkeypatch):
    good = Claim(id="c1", text="Eiffel Tower is in Paris.", category=ClaimCategory.FACTUAL)
    bad = Claim(
        id="c2",
        text="The Eiffel Tower is made of solid gold.",
        category=ClaimCategory.FACTUAL,
    )
    groundings = {
        "c1": GroundingResult(
            claim_id="c1",
            grounding_score=96,
            grounding_level=GroundingLevel.GROUNDED,
            matched_passage="The Eiffel Tower is located in Paris, France.",
            match_location=(0, 45),
            reasoning="match",
        ),
        "c2": GroundingResult(
            claim_id="c2",
            grounding_score=15,
            grounding_level=GroundingLevel.UNGROUNDED,
            matched_passage=None,
            match_location=None,
            reasoning="no support for 'solid gold' in source",
        ),
    }
    consistencies = {
        "c1": ConsistencyResult(
            claim_id="c1",
            verdict=ConsistencyVerdict.CONSISTENT,
            source_consistent=True,
            internal_consistent=True,
            confidence=10,
            reasoning="faithful",
        ),
        "c2": ConsistencyResult(
            claim_id="c2",
            verdict=ConsistencyVerdict.INCONSISTENT,
            source_consistent=False,
            internal_consistent=True,
            confidence=10,
            reasoning="fabricated material claim",
        ),
    }
    monkeypatch.setattr(
        main_module,
        "VerifyPipeline",
        _make_pipeline_factory([good, bad], groundings, consistencies),
    )

    r = client.post("/verify", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert len(body["hallucinations"]) == 1
    assert body["hallucinations"][0]["claim_id"] == "c2"
    assert body["hallucinations"][0]["is_hallucination"] is True
    assert body["overall_score"] < 90
    # Verified claim still makes it through.
    assert len(body["verified"]) == 1
    assert body["verified"][0]["claim_id"] == "c1"


def test_verify_empty_extraction_returns_perfect_score(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "VerifyPipeline",
        _make_pipeline_factory([], {}, {}),
    )

    r = client.post("/verify", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["overall_score"] == 100
    assert body["claims"] == []
    assert body["metadata"]["claim_count"] == 0


def test_verify_rejects_empty_payload():
    r = client.post("/verify", json={"source_context": "", "llm_output": ""})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"


@pytest.mark.asyncio
async def test_orchestrator_records_timings_and_parallelism():
    claim = Claim(id="c1", text="t", category=ClaimCategory.FACTUAL)
    pipeline = VerifyPipeline(
        extractor=_StubExtractor(claims=[claim]),
        grounder=_StubGrounder(
            results={
                "c1": GroundingResult(
                    claim_id="c1",
                    grounding_score=95,
                    grounding_level=GroundingLevel.GROUNDED,
                    matched_passage="x",
                    match_location=(0, 1),
                    reasoning="ok",
                )
            }
        ),
        consistency=_StubConsistency(
            results={
                "c1": ConsistencyResult(
                    claim_id="c1",
                    verdict=ConsistencyVerdict.CONSISTENT,
                    source_consistent=True,
                    internal_consistent=True,
                    confidence=10,
                    reasoning="ok",
                )
            }
        ),
    )
    report = await pipeline.run(
        VerifyRequest(source_context="s", llm_output="o")
    )
    assert report.metadata.claim_count == 1
    assert report.metadata.duration_ms >= 0
    # extractor and grounder timings were populated independently.
    assert report.metadata.extractor_ms >= 0
    assert report.metadata.grounder_ms >= 0
