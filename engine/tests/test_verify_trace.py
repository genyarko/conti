from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
from engine.app.services.audit import AuditLog, TraceStore


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


def _pipeline_factory_with_evidence():
    claim = Claim(id="c1", text="Eiffel Tower is in Paris.", category=ClaimCategory.FACTUAL)
    grounding = {
        "c1": GroundingResult(
            claim_id="c1",
            grounding_score=92,
            grounding_level=GroundingLevel.GROUNDED,
            matched_passage="The Eiffel Tower is a tower in Paris.",
            match_location=(5, 42),
            reasoning="Direct textual match against source.",
            used_semantic_fallback=False,
        )
    }
    consistency = {
        "c1": ConsistencyResult(
            claim_id="c1",
            verdict=ConsistencyVerdict.CONSISTENT,
            source_consistent=True,
            internal_consistent=True,
            confidence=9,
            reasoning="Aligned with source on location.",
            contradicts=[],
        )
    }

    def factory() -> VerifyPipeline:
        return VerifyPipeline(
            extractor=_StubExtractor(claims=[claim]),
            grounder=_StubGrounder(results=grounding),
            consistency=_StubConsistency(results=consistency),
        )

    return factory


@pytest.fixture(autouse=True)
def _fresh_trace_store(monkeypatch: pytest.MonkeyPatch):
    store = TraceStore(ttl_seconds=60, max_entries=16, enabled=True)
    monkeypatch.setattr(main_module, "_trace_store", store)
    yield store


@pytest.fixture(autouse=True)
def _tmp_audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(path=log_path, max_bytes=1024 * 1024, enabled=True)
    monkeypatch.setattr(main_module, "_audit_log", audit)
    yield audit


@pytest.fixture(autouse=True)
def _reset_metrics():
    main_module._metrics.reset()
    yield
    main_module._metrics.reset()


@pytest.fixture(autouse=True)
async def _disable_cache(monkeypatch: pytest.MonkeyPatch):
    """Force the pipeline to actually run so trace evidence is populated —
    the shared cache can otherwise short-circuit subsequent calls."""
    await main_module._report_cache.clear()
    from engine.config import settings

    monkeypatch.setattr(settings, "cache_enabled", False)
    yield


def test_verify_persists_trace_retrievable_by_request_id(monkeypatch):
    monkeypatch.setattr(main_module, "VerifyPipeline", _pipeline_factory_with_evidence())

    r = client.post("/verify", json={"source_context": "s", "llm_output": "o"})
    assert r.status_code == 200
    request_id = r.headers["X-Request-ID"]

    r2 = client.get(f"/verify/trace/{request_id}")
    assert r2.status_code == 200
    body = r2.json()

    assert body["request_id"] == request_id
    assert body["endpoint"] == "/verify"
    assert body["report"]["metadata"]["request_id"] == request_id
    assert len(body["evidence"]) == 1

    ev = body["evidence"][0]
    assert ev["claim_id"] == "c1"
    assert ev["grounding_score"] == 92
    assert ev["grounding_level"] == "grounded"
    assert ev["matched_passage"] == "The Eiffel Tower is a tower in Paris."
    assert ev["match_location"] == [5, 42]
    assert ev["grounding_reasoning"] == "Direct textual match against source."
    assert ev["used_semantic_fallback"] is False
    assert ev["consistency_verdict"] == "consistent"
    assert ev["consistency_reasoning"] == "Aligned with source on location."
    assert ev["confidence"] == 9


def test_verify_quick_persists_trace(monkeypatch):
    monkeypatch.setattr(main_module, "VerifyPipeline", _pipeline_factory_with_evidence())

    r = client.post("/verify/quick", json={"source_context": "s", "llm_output": "o"})
    assert r.status_code == 200
    request_id = r.headers["X-Request-ID"]

    r2 = client.get(f"/verify/trace/{request_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["endpoint"] == "/verify/quick"
    assert len(body["evidence"]) == 1


def test_verify_claims_persists_trace(monkeypatch):
    monkeypatch.setattr(main_module, "VerifyPipeline", _pipeline_factory_with_evidence())

    payload = {
        "source_context": "s",
        "claims": [{"id": "c1", "text": "Eiffel Tower is in Paris.", "category": "factual"}],
    }
    r = client.post("/verify/claims", json=payload)
    assert r.status_code == 200
    request_id = r.headers["X-Request-ID"]

    r2 = client.get(f"/verify/trace/{request_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["endpoint"] == "/verify/claims"
    assert body["evidence"][0]["claim_id"] == "c1"


def test_verify_batch_persists_trace_per_item(monkeypatch):
    monkeypatch.setattr(main_module, "VerifyPipeline", _pipeline_factory_with_evidence())

    payload = {
        "items": [
            {"source_context": f"s{i}", "llm_output": f"o{i}"} for i in range(2)
        ],
    }
    r = client.post("/verify/batch", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 2

    for item in body["results"]:
        item_request_id = item["report"]["metadata"]["request_id"]
        r2 = client.get(f"/verify/trace/{item_request_id}")
        assert r2.status_code == 200, f"trace missing for {item_request_id}"
        trace_body = r2.json()
        assert trace_body["endpoint"] == "/verify/batch"
        assert trace_body["request_id"] == item_request_id


def test_trace_404_when_unknown_request_id():
    r = client.get("/verify/trace/req_nonexistent")
    assert r.status_code == 404
    assert r.json()["error"] == "trace_not_found"


def test_trace_404_when_store_disabled(monkeypatch):
    monkeypatch.setattr(main_module, "VerifyPipeline", _pipeline_factory_with_evidence())
    # Disable the store entirely — saves become no-ops, gets return None.
    disabled = TraceStore(ttl_seconds=60, max_entries=16, enabled=False)
    monkeypatch.setattr(main_module, "_trace_store", disabled)

    r = client.post("/verify", json={"source_context": "s", "llm_output": "o"})
    assert r.status_code == 200
    request_id = r.headers["X-Request-ID"]

    r2 = client.get(f"/verify/trace/{request_id}")
    assert r2.status_code == 404


def test_trace_404_when_other_tenant_owns_request_id(monkeypatch):
    """A second caller with a different api_key_id must not see another
    tenant's trace — the lookup returns 404, identical to the not-found
    response, to avoid disclosing existence across keys."""
    monkeypatch.setattr(main_module, "VerifyPipeline", _pipeline_factory_with_evidence())

    # Use a fresh store and stamp the saved trace with a specific api_key_id
    # that won't match the request.state.api_key_id used by the lookup.
    store = TraceStore(ttl_seconds=60, max_entries=16, enabled=True)
    monkeypatch.setattr(main_module, "_trace_store", store)

    r = client.post("/verify", json={"source_context": "s", "llm_output": "o"})
    assert r.status_code == 200
    request_id = r.headers["X-Request-ID"]

    # First call is owned by api_key_id="default" (no auth configured).
    # Manually re-tag the saved entry to simulate a different owner so the
    # lookup runs the filter.
    with store._lock:
        stored_at, trace, _ = store._data[request_id]
        store._data[request_id] = (stored_at, trace, "alice")

    r2 = client.get(f"/verify/trace/{request_id}")
    assert r2.status_code == 404
    assert r2.json()["error"] == "trace_not_found"


async def test_trace_store_filters_by_api_key_id_in_memory():
    from engine.app.models.schemas import (
        IntegrityReport,
        ReportMetadata,
        VerifyTrace,
    )

    store = TraceStore(ttl_seconds=60, max_entries=16, enabled=True)

    def make_trace(rid: str) -> VerifyTrace:
        md = ReportMetadata(model="test", request_id=rid)
        return VerifyTrace(
            request_id=rid,
            endpoint="/verify",
            report=IntegrityReport(overall_score=100, metadata=md),
            evidence=[],
        )

    await store.save(make_trace("req_a"), api_key_id="alice")
    await store.save(make_trace("req_b"), api_key_id="bob")

    assert (await store.get("req_a", api_key_id="alice")) is not None
    assert (await store.get("req_a", api_key_id="bob")) is None
    assert (await store.get("req_b", api_key_id="bob")) is not None
    # Internal callers (no key supplied) still see everything.
    assert (await store.get("req_a")) is not None


async def test_audit_log_filters_by_api_key_id_in_memory(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(path=log_path, max_bytes=1024 * 1024, enabled=True)

    await audit.append({
        "request_id": "r_alice",
        "api_key_id": "alice",
        "endpoint": "/verify",
        "timestamp": "2026-04-29T12:00:00.000Z",
    })
    await audit.append({
        "request_id": "r_bob",
        "api_key_id": "bob",
        "endpoint": "/verify",
        "timestamp": "2026-04-29T12:01:00.000Z",
    })

    alice = await audit.read_tail(limit=10, api_key_id="alice")
    assert [r["request_id"] for r in alice] == ["r_alice"]
    bob = await audit.read_tail(limit=10, api_key_id="bob")
    assert [r["request_id"] for r in bob] == ["r_bob"]


async def test_trace_store_ttl_evicts_entries():
    store = TraceStore(ttl_seconds=60, max_entries=3, enabled=True)
    from engine.app.models.schemas import (
        IntegrityReport,
        ReportMetadata,
        VerifyTrace,
    )

    def make_trace(rid: str) -> VerifyTrace:
        md = ReportMetadata(model="test", request_id=rid)
        return VerifyTrace(
            request_id=rid,
            endpoint="/verify",
            report=IntegrityReport(overall_score=100, metadata=md),
            evidence=[],
        )

    for i in range(5):
        await store.save(make_trace(f"req_{i}"))

    # Only the 3 most recent should be retained.
    assert await store.get("req_0") is None
    assert await store.get("req_1") is None
    assert await store.get("req_4") is not None
    assert await store.get("req_3") is not None
    assert await store.get("req_2") is not None
