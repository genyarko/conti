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
)
from engine.app.pipeline.consistency import ConsistencyResult
from engine.app.pipeline.extractor import ExtractionResult
from engine.app.pipeline.grounder import GroundingResult
from engine.app.pipeline.orchestrator import VerifyPipeline

client = TestClient(app)


@dataclass
class _StubExtractor:
    claims: list[Claim]
    calls: int = 0

    async def extract(self, llm_output: str) -> ExtractionResult:
        self.calls += 1
        return ExtractionResult(claims=self.claims, raw_responses=[])


@dataclass
class _StubGrounder:
    results: dict[str, GroundingResult]
    calls: int = 0

    async def ground_many(self, claims, source_context):
        self.calls += 1
        return [self.results[c.id] for c in claims]


@dataclass
class _StubConsistency:
    results: dict[str, ConsistencyResult] = field(default_factory=dict)
    calls: int = 0

    async def check(self, claims, source_context):
        self.calls += 1
        return [self.results[c.id] for c in claims]


def _install_pipeline(monkeypatch, *, extractor=None, grounder=None, consistency=None):
    def _factory() -> VerifyPipeline:
        return VerifyPipeline(
            extractor=extractor,
            grounder=grounder,
            consistency=consistency,
        )

    monkeypatch.setattr(main_module, "VerifyPipeline", _factory)


def _clear_cache_and_rate_limits():
    main_module._report_cache.clear()
    main_module._rate_limiter.reset()


@pytest.fixture(autouse=True)
def _reset_state():
    _clear_cache_and_rate_limits()
    yield
    _clear_cache_and_rate_limits()


def _grounded(cid: str, score: int = 95) -> GroundingResult:
    return GroundingResult(
        claim_id=cid,
        grounding_score=score,
        grounding_level=(
            GroundingLevel.GROUNDED
            if score >= 90
            else GroundingLevel.PARTIALLY_GROUNDED
            if score >= 70
            else GroundingLevel.UNGROUNDED
        ),
        matched_passage="match" if score >= 70 else None,
        match_location=(0, 5) if score >= 70 else None,
        reasoning="ok",
    )


def _consistent(cid: str) -> ConsistencyResult:
    return ConsistencyResult(
        claim_id=cid,
        verdict=ConsistencyVerdict.CONSISTENT,
        source_consistent=True,
        internal_consistent=True,
        confidence=10,
        reasoning="ok",
    )


# ---------- /verify/quick ----------


def test_verify_quick_skips_consistency(monkeypatch):
    claim = Claim(id="c1", text="Eiffel Tower is in Paris.", category=ClaimCategory.FACTUAL)
    extractor = _StubExtractor(claims=[claim])
    grounder = _StubGrounder(results={"c1": _grounded("c1", 96)})
    consistency = _StubConsistency()  # should NOT be called
    _install_pipeline(monkeypatch, extractor=extractor, grounder=grounder, consistency=consistency)

    r = client.post(
        "/verify/quick",
        json={"source_context": "src", "llm_output": "out"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["overall_score"] >= 90
    assert len(body["verified"]) == 1
    assert body["metadata"]["consistency_ms"] == 0
    assert extractor.calls == 1
    assert grounder.calls == 1
    assert consistency.calls == 0


def test_verify_quick_ungrounded_claim_is_flagged_not_hallucination(monkeypatch):
    claim = Claim(id="c1", text="Bogus claim", category=ClaimCategory.FACTUAL)
    extractor = _StubExtractor(claims=[claim])
    grounder = _StubGrounder(results={"c1": _grounded("c1", 10)})
    _install_pipeline(
        monkeypatch, extractor=extractor, grounder=grounder, consistency=_StubConsistency()
    )

    r = client.post(
        "/verify/quick",
        json={"source_context": "src", "llm_output": "out"},
    )
    assert r.status_code == 200
    body = r.json()
    # Without consistency, no hallucinations can be asserted.
    assert body["hallucinations"] == []
    assert len(body["flagged"]) == 1


# ---------- /verify/claims ----------


def test_verify_claims_skips_extraction(monkeypatch):
    extractor = _StubExtractor(claims=[])  # should NOT be called
    grounder = _StubGrounder(
        results={
            "caller-1": _grounded("caller-1", 95),
            "caller-2": _grounded("caller-2", 80),
        }
    )
    consistency = _StubConsistency(
        results={
            "caller-1": _consistent("caller-1"),
            "caller-2": _consistent("caller-2"),
        }
    )
    _install_pipeline(monkeypatch, extractor=extractor, grounder=grounder, consistency=consistency)

    payload = {
        "source_context": "source",
        "claims": [
            {"id": "caller-1", "text": "A", "category": "factual"},
            {"id": "caller-2", "text": "B", "category": "interpretive"},
        ],
    }
    r = client.post("/verify/claims", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["metadata"]["claim_count"] == 2
    assert body["metadata"]["extractor_ms"] == 0
    assert extractor.calls == 0
    assert grounder.calls == 1
    assert consistency.calls == 1


def test_verify_claims_rejects_empty_list():
    r = client.post(
        "/verify/claims",
        json={"source_context": "x", "claims": []},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


# ---------- caching ----------


def test_verify_quick_cache_hit_avoids_pipeline(monkeypatch):
    claim = Claim(id="c1", text="A", category=ClaimCategory.FACTUAL)
    extractor = _StubExtractor(claims=[claim])
    grounder = _StubGrounder(results={"c1": _grounded("c1", 95)})
    _install_pipeline(
        monkeypatch, extractor=extractor, grounder=grounder, consistency=_StubConsistency()
    )

    body = {"source_context": "same", "llm_output": "same"}
    r1 = client.post("/verify/quick", json=body)
    r2 = client.post("/verify/quick", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert extractor.calls == 1  # second hit served from cache
    assert grounder.calls == 1


# ---------- rate limiting ----------


def test_rate_limit_returns_429(monkeypatch):
    _install_pipeline(
        monkeypatch,
        extractor=_StubExtractor(claims=[]),
        grounder=_StubGrounder(results={}),
        consistency=_StubConsistency(),
    )
    monkeypatch.setattr(main_module.settings, "rate_limit_per_minute", 2)
    monkeypatch.setattr(main_module._rate_limiter, "_limit", 2)
    _clear_cache_and_rate_limits()

    # Vary payloads so we don't hit the response cache.
    for i in range(2):
        r = client.post(
            "/verify/quick",
            json={"source_context": f"s{i}", "llm_output": f"o{i}"},
        )
        assert r.status_code == 200

    r = client.post(
        "/verify/quick",
        json={"source_context": "s2", "llm_output": "o2"},
    )
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "rate_limited"
    assert "retry_after_seconds" in body
    assert r.headers.get("Retry-After")


# ---------- size limits ----------


def test_oversize_payload_returns_413(monkeypatch):
    monkeypatch.setattr(main_module.settings, "max_input_chars", 20)
    _install_pipeline(
        monkeypatch,
        extractor=_StubExtractor(claims=[]),
        grounder=_StubGrounder(results={}),
        consistency=_StubConsistency(),
    )

    big = "x" * 50
    r = client.post("/verify", json={"source_context": big, "llm_output": big})
    assert r.status_code == 413
    assert r.json()["error"] == "payload_too_large"


def test_too_many_claims_returns_413(monkeypatch):
    monkeypatch.setattr(main_module.settings, "max_claims_per_request", 2)
    _install_pipeline(
        monkeypatch,
        extractor=_StubExtractor(claims=[]),
        grounder=_StubGrounder(results={}),
        consistency=_StubConsistency(),
    )

    claims = [{"text": f"claim {i}"} for i in range(3)]
    r = client.post(
        "/verify/claims",
        json={"source_context": "src", "claims": claims},
    )
    assert r.status_code == 413
    assert r.json()["error"] == "too_many_claims"
