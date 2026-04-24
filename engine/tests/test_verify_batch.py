from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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


def _happy_pipeline_factory(*, tokens: tuple[int, int] = (50, 20), cost: float = 0.002):
    claim = Claim(id="c1", text="t", category=ClaimCategory.FACTUAL)
    grounding = {
        "c1": GroundingResult(
            claim_id="c1",
            grounding_score=95,
            grounding_level=GroundingLevel.GROUNDED,
            matched_passage="x",
            match_location=(0, 1),
            reasoning="ok",
        )
    }
    consistency = {
        "c1": ConsistencyResult(
            claim_id="c1",
            verdict=ConsistencyVerdict.CONSISTENT,
            source_consistent=True,
            internal_consistent=True,
            confidence=10,
            reasoning="ok",
        )
    }

    def factory() -> VerifyPipeline:
        pipeline = VerifyPipeline(
            extractor=_StubExtractor(claims=[claim]),
            grounder=_StubGrounder(results=grounding),
            consistency=_StubConsistency(results=consistency),
        )
        original_run = pipeline.run

        async def run_with_tokens(request):
            report = await original_run(request)
            report.metadata.input_tokens = tokens[0]
            report.metadata.output_tokens = tokens[1]
            report.metadata.total_tokens = sum(tokens)
            report.metadata.estimated_cost_usd = cost
            return report

        pipeline.run = run_with_tokens  # type: ignore[assignment]
        return pipeline

    return factory


def _hallucinating_pipeline_factory():
    good = Claim(id="c1", text="g", category=ClaimCategory.FACTUAL)
    bad = Claim(id="c2", text="b", category=ClaimCategory.FACTUAL)
    grounding = {
        "c1": GroundingResult(
            claim_id="c1",
            grounding_score=95,
            grounding_level=GroundingLevel.GROUNDED,
            matched_passage="x",
            match_location=(0, 1),
            reasoning="ok",
        ),
        "c2": GroundingResult(
            claim_id="c2",
            grounding_score=10,
            grounding_level=GroundingLevel.UNGROUNDED,
            matched_passage=None,
            match_location=None,
            reasoning="no support",
        ),
    }
    consistency = {
        "c1": ConsistencyResult(
            claim_id="c1",
            verdict=ConsistencyVerdict.CONSISTENT,
            source_consistent=True,
            internal_consistent=True,
            confidence=10,
            reasoning="ok",
        ),
        "c2": ConsistencyResult(
            claim_id="c2",
            verdict=ConsistencyVerdict.INCONSISTENT,
            source_consistent=False,
            internal_consistent=True,
            confidence=10,
            reasoning="fabricated",
        ),
    }

    def factory() -> VerifyPipeline:
        return VerifyPipeline(
            extractor=_StubExtractor(claims=[good, bad]),
            grounder=_StubGrounder(results=grounding),
            consistency=_StubConsistency(results=consistency),
        )

    return factory


def _exploding_pipeline_factory():
    def factory() -> VerifyPipeline:
        pipeline = VerifyPipeline(
            extractor=_StubExtractor(claims=[]),
            grounder=_StubGrounder(results={}),
            consistency=_StubConsistency(results={}),
        )

        async def boom(request):
            raise RuntimeError("stubbed failure")

        pipeline.run = boom  # type: ignore[assignment]
        return pipeline

    return factory


def _payload(n: int, *, mode: str = "full") -> dict:
    return {
        "mode": mode,
        "items": [
            {"source_context": f"s-{i}", "llm_output": f"o-{i}"}
            for i in range(n)
        ],
    }


@pytest.fixture(autouse=True)
def _reset_metrics():
    main_module._metrics.reset()
    yield
    main_module._metrics.reset()


def test_batch_happy_path_returns_per_item_results_and_rollup(monkeypatch):
    monkeypatch.setattr(
        main_module, "VerifyPipeline", _happy_pipeline_factory(tokens=(100, 25))
    )

    r = client.post("/verify/batch", json=_payload(3))
    assert r.status_code == 200
    body = r.json()

    assert len(body["results"]) == 3
    assert all(item["status"] == "ok" for item in body["results"])
    assert [item["index"] for item in body["results"]] == [0, 1, 2]

    rollup = body["rollup"]
    assert rollup["item_count"] == 3
    assert rollup["ok_count"] == 3
    assert rollup["error_count"] == 0
    assert rollup["hallucination_item_count"] == 0
    assert rollup["total_input_tokens"] == 300
    assert rollup["total_output_tokens"] == 75
    assert rollup["total_tokens"] == 375
    assert rollup["mode"] == "full"
    assert rollup["concurrency"] >= 1


def test_batch_isolates_per_item_failures(monkeypatch):
    monkeypatch.setattr(main_module, "VerifyPipeline", _exploding_pipeline_factory())

    r = client.post("/verify/batch", json=_payload(2))
    assert r.status_code == 200
    body = r.json()

    assert body["rollup"]["ok_count"] == 0
    assert body["rollup"]["error_count"] == 2
    for item in body["results"]:
        assert item["status"] == "error"
        assert item["error"]["code"] == "RuntimeError"
        assert item["error"]["message"] == "Batch item processing failed."


def test_batch_counts_hallucination_items(monkeypatch):
    monkeypatch.setattr(main_module, "VerifyPipeline", _hallucinating_pipeline_factory())

    r = client.post("/verify/batch", json=_payload(2))
    assert r.status_code == 200
    body = r.json()
    assert body["rollup"]["hallucination_item_count"] == 2


def test_batch_rejects_exceeding_max_items(monkeypatch):
    monkeypatch.setattr(main_module.settings, "batch_max_items", 3)
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    r = client.post("/verify/batch", json=_payload(4))
    assert r.status_code == 413
    body = r.json()
    assert body["error"] == "too_many_items"


def test_batch_rejects_oversize_item(monkeypatch):
    monkeypatch.setattr(main_module.settings, "max_input_chars", 20)
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    payload = {
        "items": [
            {"source_context": "ok", "llm_output": "ok"},
            {"source_context": "x" * 30, "llm_output": "y" * 30},
        ]
    }
    r = client.post("/verify/batch", json=payload)
    assert r.status_code == 413
    body = r.json()
    assert body["error"] == "payload_too_large"
    assert body["item_index"] == 1


def test_batch_validation_error_on_empty_items_list():
    r = client.post("/verify/batch", json={"items": []})
    assert r.status_code == 422


def test_batch_stats_rollup_reflects_batch_cost(monkeypatch):
    monkeypatch.setattr(
        main_module, "VerifyPipeline", _happy_pipeline_factory(tokens=(10, 5), cost=0.01)
    )
    r = client.post("/verify/batch", json=_payload(4))
    assert r.status_code == 200

    stats = client.get("/stats").json()["metrics"]
    verify_batch = stats["endpoints"]["/verify/batch"]
    assert verify_batch["request_count"] == 1
    # Single batch call aggregates all 4 items' cost into /stats.
    assert stats["total"]["estimated_cost_usd"] == pytest.approx(0.04, rel=1e-3)
    assert stats["total"]["total_tokens"] == 60  # (10 + 5) * 4


def test_batch_quick_mode_uses_run_quick(monkeypatch):
    called: dict[str, int] = {"run": 0, "run_quick": 0}

    def factory() -> VerifyPipeline:
        pipeline = VerifyPipeline(
            extractor=_StubExtractor(claims=[]),
            grounder=_StubGrounder(results={}),
            consistency=_StubConsistency(results={}),
        )

        async def fake_run(request):
            called["run"] += 1
            return await original_run(request)

        async def fake_run_quick(request):
            called["run_quick"] += 1
            return await original_run_quick(request)

        original_run = pipeline.run
        original_run_quick = pipeline.run_quick
        pipeline.run = fake_run  # type: ignore[assignment]
        pipeline.run_quick = fake_run_quick  # type: ignore[assignment]
        return pipeline

    monkeypatch.setattr(main_module, "VerifyPipeline", factory)

    r = client.post("/verify/batch", json=_payload(2, mode="quick"))
    assert r.status_code == 200
    assert called["run_quick"] == 2
    assert called["run"] == 0
