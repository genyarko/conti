"""Tests that the orchestrator and batch audit aggregator correctly combine
Lobster Trap signals across multiple LLM calls. These guard the two bugs
that originally made the security signals invisible:

    1. Within-stage events overwriting each other on the same client.
    2. Batch audit row only reading the first item's metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from engine.app.models.schemas import ReportMetadata
from engine.app.pipeline.orchestrator import VerifyPipeline


@dataclass
class _ClientWithEvents:
    """Stand-in for a real LLM client; only the bits `_apply_security` reads."""
    security_events: list[dict] = field(default_factory=list)


@dataclass
class _StageWithClient:
    """Mimics ClaimExtractor/ClaimGrounder/ConsistencyChecker just enough for
    the `_stage_clients` helper to find a `_client` attribute on it."""
    _client: Optional[_ClientWithEvents]


def _meta() -> ReportMetadata:
    return ReportMetadata(model="claude-haiku-4-5", fast_model="claude-haiku-4-5")


def test_apply_security_escalates_to_highest_risk_across_calls():
    """A High in any call must beat a Medium or Low in any other call,
    regardless of order. Guards bug #2 (within-stage overwrite)."""
    pipeline = VerifyPipeline()
    client = _ClientWithEvents(security_events=[
        {"risk_score": "Low", "intent_detected": "x"},
        {"risk_score": "High", "intent_detected": "exploit"},
        {"risk_score": "Medium", "intent_detected": "y"},
    ])
    stage = _StageWithClient(_client=client)
    metadata = _meta()
    pipeline._apply_security(metadata, [client])
    assert metadata.security_risk_score == "High"


def test_apply_security_or_reduces_mismatch():
    pipeline = VerifyPipeline()
    client = _ClientWithEvents(security_events=[
        {"intent_mismatch": False, "intent_detected": "ok"},
        {"intent_mismatch": True, "intent_declared": "grounding",
         "intent_detected": "data_exfiltration"},
    ])
    metadata = _meta()
    pipeline._apply_security(metadata, [client])
    assert metadata.security_intent_mismatch is True
    # Surfaced labels prefer the mismatched event.
    assert metadata.security_intent_declared == "grounding"
    assert metadata.security_intent_detected == "data_exfiltration"


def test_apply_security_aggregates_across_multiple_clients():
    """Different pipeline stages have separate client instances. The
    orchestrator must walk all of them, not just the first."""
    pipeline = VerifyPipeline()
    extractor_client = _ClientWithEvents(security_events=[
        {"risk_score": "Low", "intent_detected": "ok"},
    ])
    grounder_client = _ClientWithEvents(security_events=[
        {"risk_score": "High", "intent_detected": "exploit",
         "action": "QUARANTINE"},
    ])
    metadata = _meta()
    pipeline._apply_security(metadata, [extractor_client, grounder_client])
    assert metadata.security_risk_score == "High"
    assert metadata.security_action == "QUARANTINE"


def test_apply_security_handles_missing_clients_gracefully():
    """`_stage_clients` returns None for stages without a `_client` (e.g.,
    test stubs). `_apply_security` must skip those, not AttributeError."""
    pipeline = VerifyPipeline()
    metadata = _meta()
    pipeline._apply_security(metadata, [None, None])
    assert metadata.security_risk_score is None
    assert metadata.security_intent_mismatch is False


def test_apply_security_ignores_clients_without_events_attr():
    """A client that pre-dates the `security_events` field (or a fully
    foreign object) shouldn't crash the aggregator."""
    pipeline = VerifyPipeline()
    foreign = object()
    metadata = _meta()
    pipeline._apply_security(metadata, [foreign])  # type: ignore[list-item]
    assert metadata.security_risk_score is None


# --- Batch audit aggregation -------------------------------------------------

def test_batch_audit_picks_highest_risk_across_items(monkeypatch):
    """Reproduces the original bug: when item 0 is Low/None and item 5 is
    High, the batch audit row used to only see item 0 (because of `break`).
    Now it should escalate."""
    from engine.app import main as main_module

    # Build three fake reports with risk scores in non-monotonic order.
    item0 = _make_item_result(risk="Low", mismatch=False, action=None)
    item1 = _make_item_result(risk="High", mismatch=True, action="DENY")
    item2 = _make_item_result(risk="Medium", mismatch=False, action="LOG")

    audited: dict = {}

    async def _fake_append(record):
        audited.update(record)

    class _FakeAuditLog:
        enabled = True
        append = staticmethod(_fake_append)

    monkeypatch.setattr(main_module, "_audit_log", _FakeAuditLog())

    rollup = _make_rollup()
    http_request = _FakeHttpRequest()

    import asyncio
    asyncio.run(
        main_module._emit_audit_for_batch(
            http_request,
            batch_id="batch-1",
            rollup=rollup,
            results=[item0, item1, item2],
            duration_ms=42,
        )
    )

    assert audited["security_risk_score"] == "High"
    assert audited["security_intent_mismatch"] is True
    # First non-empty action wins, but we should pick up SOME action.
    assert audited["security_action"] in ("DENY", "LOG")


# --- helpers ---

def _make_item_result(*, risk, mismatch, action):
    """Construct a minimal BatchItemResult enough for `_emit_audit_for_batch`."""
    from engine.app.models.schemas import BatchItemResult, IntegrityReport
    md = ReportMetadata(model="m", fast_model="m")
    md.security_risk_score = risk
    md.security_intent_mismatch = mismatch
    md.security_action = action
    report = IntegrityReport(
        overall_score=100,
        verified=[], uncertain=[], flagged=[], hallucinations=[], claims=[],
        metadata=md,
    )
    return BatchItemResult(index=0, status="ok", report=report, error=None)


def _make_rollup():
    from engine.app.models.schemas import BatchRollup
    return BatchRollup(
        item_count=3, ok_count=3, error_count=0,
        hallucination_item_count=0, mode="full",
        total_input_tokens=0, total_output_tokens=0,
        total_tokens=0, estimated_cost_usd=0.0,
        duration_ms=42, concurrency=1,
    )


@dataclass
class _FakeHttpRequest:
    class _State:
        request_id: str = ""
        api_key_id: str = "default"
    state: _State = field(default_factory=_State)
