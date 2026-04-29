from __future__ import annotations

import json
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
from engine.app.services.audit import AuditLog


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


def _grounded(claim_id: str) -> GroundingResult:
    return GroundingResult(
        claim_id=claim_id,
        grounding_score=95,
        grounding_level=GroundingLevel.GROUNDED,
        matched_passage="x",
        match_location=(0, 1),
        reasoning="ok",
    )


def _consistent(claim_id: str) -> ConsistencyResult:
    return ConsistencyResult(
        claim_id=claim_id,
        verdict=ConsistencyVerdict.CONSISTENT,
        source_consistent=True,
        internal_consistent=True,
        confidence=10,
        reasoning="ok",
    )


def _happy_pipeline_factory(*, claim_ids: tuple[str, ...] = ("c1",)):
    claims = [Claim(id=cid, text=cid, category=ClaimCategory.FACTUAL) for cid in claim_ids]
    grounding = {cid: _grounded(cid) for cid in claim_ids}
    consistency = {cid: _consistent(cid) for cid in claim_ids}

    def factory() -> VerifyPipeline:
        return VerifyPipeline(
            extractor=_StubExtractor(claims=list(claims)),
            grounder=_StubGrounder(results=grounding),
            consistency=_StubConsistency(results=consistency),
        )

    return factory


@pytest.fixture(autouse=True)
def _tmp_audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the global audit log to a per-test tmp file so tests don't
    pollute (or read) the real audit file."""
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(path=log_path, max_bytes=1024 * 1024, enabled=True)
    monkeypatch.setattr(main_module, "_audit_log", audit)
    yield audit


@pytest.fixture(autouse=True)
def _reset_metrics():
    main_module._metrics.reset()
    yield
    main_module._metrics.reset()


def _read_records(audit: AuditLog) -> list[dict]:
    if not audit.path.exists():
        return []
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_verify_writes_one_audit_line_and_sets_request_id_header(
    monkeypatch, _tmp_audit_log
):
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    r = client.post("/verify", json={"source_context": "s", "llm_output": "o"})
    assert r.status_code == 200

    request_id = r.headers.get("X-Request-ID")
    assert request_id, "X-Request-ID header must be surfaced on /verify responses"

    records = _read_records(_tmp_audit_log)
    assert len(records) == 1
    record = records[0]
    assert record["request_id"] == request_id
    assert record["endpoint"] == "/verify"
    assert record["status_code"] == 200
    assert record["claim_count"] == 1
    assert record["outcome_counts"]["verified"] == 1
    assert "timestamp" in record
    assert isinstance(record["estimated_cost_usd"], (int, float))


def test_verify_quick_writes_audit_line(monkeypatch, _tmp_audit_log):
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    r = client.post("/verify/quick", json={"source_context": "s", "llm_output": "o"})
    assert r.status_code == 200

    records = _read_records(_tmp_audit_log)
    assert len(records) == 1
    assert records[0]["endpoint"] == "/verify/quick"


def test_verify_claims_writes_audit_line(monkeypatch, _tmp_audit_log):
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    payload = {
        "source_context": "s",
        "claims": [{"id": "c1", "text": "t", "category": "factual"}],
    }
    r = client.post("/verify/claims", json=payload)
    assert r.status_code == 200

    records = _read_records(_tmp_audit_log)
    assert len(records) == 1
    assert records[0]["endpoint"] == "/verify/claims"


def test_verify_batch_writes_single_aggregate_audit_line(
    monkeypatch, _tmp_audit_log
):
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    payload = {
        "items": [
            {"source_context": f"s{i}", "llm_output": f"o{i}"} for i in range(3)
        ],
    }
    r = client.post("/verify/batch", json=payload)
    assert r.status_code == 200

    batch_id = r.headers.get("X-Request-ID")
    assert batch_id and batch_id.startswith("batch_")

    records = _read_records(_tmp_audit_log)
    assert len(records) == 1
    record = records[0]
    assert record["request_id"] == batch_id
    assert record["endpoint"] == "/verify/batch"
    assert record["item_count"] == 3
    assert record["ok_count"] == 3
    assert record["claim_count"] == 3  # one claim per item


def test_audit_events_returns_newest_records(monkeypatch, _tmp_audit_log):
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    for i in range(5):
        client.post("/verify", json={"source_context": f"s{i}", "llm_output": f"o{i}"})

    r = client.get("/audit/events?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert len(body["events"]) == 3
    for event in body["events"]:
        assert event["endpoint"] == "/verify"


def test_audit_events_filters_by_endpoint(monkeypatch, _tmp_audit_log):
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    client.post("/verify", json={"source_context": "s1", "llm_output": "o1"})
    client.post("/verify/quick", json={"source_context": "s2", "llm_output": "o2"})
    client.post("/verify/quick", json={"source_context": "s3", "llm_output": "o3"})

    r = client.get("/audit/events?endpoint=/verify/quick&limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert all(e["endpoint"] == "/verify/quick" for e in body["events"])


def test_audit_events_rejects_unknown_endpoint(_tmp_audit_log):
    r = client.get("/audit/events?endpoint=/bogus")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_endpoint"


def test_audit_events_rejects_invalid_since(_tmp_audit_log):
    r = client.get("/audit/events?since=not-a-date")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_since"


def test_audit_events_filters_by_since(monkeypatch, _tmp_audit_log):
    monkeypatch.setattr(main_module, "VerifyPipeline", _happy_pipeline_factory())

    client.post("/verify", json={"source_context": "s1", "llm_output": "o1"})
    client.post("/verify", json={"source_context": "s2", "llm_output": "o2"})

    records = _read_records(_tmp_audit_log)
    assert len(records) == 2
    # since == second record's timestamp should include only the second record.
    cutoff = records[1]["timestamp"]

    r = client.get(f"/audit/events?since={cutoff}")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["timestamp"] == cutoff


async def test_audit_log_rotation_drops_oldest_lines(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    # Tiny cap so a handful of appends triggers a rotation.
    audit = AuditLog(path=path, max_bytes=1024, enabled=True)
    for i in range(200):
        await audit.append({"seq": i, "payload": "x" * 50})

    assert path.stat().st_size <= 1024
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # The most recent writes must survive; earliest writes must have been dropped.
    assert records, "expected surviving records after rotation"
    assert records[-1]["seq"] == 199
    assert records[0]["seq"] > 0


async def test_audit_log_disabled_is_noop(tmp_path: Path):
    # Avoid the autouse fixture's audit.jsonl path — use a distinct filename.
    path = tmp_path / "disabled.jsonl"
    audit = AuditLog(path=path, max_bytes=1024, enabled=False)
    await audit.append({"seq": 1})
    assert not path.exists()
    assert await audit.read_tail(limit=10) == []
