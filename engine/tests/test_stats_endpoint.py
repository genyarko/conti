from __future__ import annotations

from dataclasses import dataclass
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
    IntegrityReport,
    ReportMetadata,
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


def _inject_pipeline(monkeypatch: pytest.MonkeyPatch, report_tokens: tuple[int, int]):
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

    def _factory() -> VerifyPipeline:
        pipeline = VerifyPipeline(
            extractor=_StubExtractor(claims=[claim]),
            grounder=_StubGrounder(results=grounding),
            consistency=_StubConsistency(results=consistency),
        )
        original_run = pipeline.run

        async def run_with_tokens(request):
            report = await original_run(request)
            report.metadata.input_tokens = report_tokens[0]
            report.metadata.output_tokens = report_tokens[1]
            report.metadata.total_tokens = sum(report_tokens)
            report.metadata.estimated_cost_usd = 0.001
            return report

        pipeline.run = run_with_tokens  # type: ignore[assignment]
        return pipeline

    monkeypatch.setattr(main_module, "VerifyPipeline", _factory)


@pytest.fixture(autouse=True)
def _reset_metrics():
    main_module._metrics.reset()
    yield
    main_module._metrics.reset()


def test_stats_endpoint_returns_zero_state_when_idle():
    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["total"]["request_count"] == 0
    assert body["metrics"]["total"]["estimated_cost_usd"] == 0.0
    assert body["cache"]["hit_rate"] == 0.0


def test_stats_endpoint_aggregates_after_verify(monkeypatch):
    _inject_pipeline(monkeypatch, report_tokens=(120, 30))

    payload = {"source_context": "s", "llm_output": "o"}
    r1 = client.post("/verify", json=payload)
    assert r1.status_code == 200

    r2 = client.get("/stats")
    assert r2.status_code == 200
    snapshot = r2.json()["metrics"]

    assert snapshot["total"]["request_count"] == 1
    assert snapshot["total"]["error_count"] == 0
    assert snapshot["total"]["input_tokens"] == 120
    assert snapshot["total"]["output_tokens"] == 30
    assert snapshot["total"]["total_tokens"] == 150
    assert snapshot["total"]["estimated_cost_usd"] == pytest.approx(0.001)

    verify = snapshot["endpoints"]["/verify"]
    assert verify["request_count"] == 1
    # p50/p95/p99 all resolve to the sole sample.
    assert verify["latency_ms"]["p50"] is not None


def test_stats_endpoint_counts_validation_errors(monkeypatch):
    # Validation error — no report body, but the request should still show up
    # in error_count for its endpoint.
    r = client.post("/verify", json={"source_context": "", "llm_output": ""})
    assert r.status_code == 422

    snapshot = client.get("/stats").json()["metrics"]
    assert snapshot["total"]["request_count"] == 1
    assert snapshot["total"]["error_count"] == 1
    assert snapshot["endpoints"]["/verify"]["error_rate"] == 1.0


def test_stats_cost_per_1k_requests_rollup(monkeypatch):
    _inject_pipeline(monkeypatch, report_tokens=(100, 50))
    payload = {"source_context": "s", "llm_output": "o"}

    # Fire 3 requests; cache dedupes identical payload, so vary them.
    for i in range(3):
        p = {"source_context": f"s-{i}", "llm_output": f"o-{i}"}
        assert client.post("/verify", json=p).status_code == 200

    rollup = client.get("/stats").json()["metrics"]["total"]
    assert rollup["request_count"] == 3
    # 0.001 per request → 1.0 per 1k.
    assert rollup["estimated_cost_per_1k_requests_usd"] == pytest.approx(1.0)
