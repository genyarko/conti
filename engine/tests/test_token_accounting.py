from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import pytest

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
from engine.app.services.anthropic_client import TokenLedger, TokenUsage
from engine.app.services.pricing import estimate_cost_usd


@dataclass
class _UsageReportingClient:
    """Fake client that returns canned responses and exposes a growing
    `last_usage` attribute, mimicking the real AnthropicClient contract."""

    responses: list[str]
    per_call_usage: TokenUsage
    calls: list[dict] = field(default_factory=list)
    last_usage: TokenUsage = field(default_factory=TokenUsage)
    security_events: list[dict] = field(default_factory=list)
    _idx: int = 0

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        response_schema: dict | None = None,
        lobstertrap_metadata: dict | None = None,
    ) -> str:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "model": model,
                "max_tokens": max_tokens,
                "response_schema": response_schema,
                "lobstertrap_metadata": lobstertrap_metadata,
            }
        )
        if self._idx >= len(self.responses):
            raise AssertionError("FakeClient ran out of canned responses")
        out = self.responses[self._idx]
        self._idx += 1
        self.last_usage = TokenUsage(
            input_tokens=self.per_call_usage.input_tokens,
            output_tokens=self.per_call_usage.output_tokens,
        )
        return out


def test_token_ledger_add_accumulates():
    ledger = TokenLedger()
    client_a = _UsageReportingClient(
        responses=[],
        per_call_usage=TokenUsage(input_tokens=100, output_tokens=50),
    )
    client_a.last_usage = TokenUsage(input_tokens=100, output_tokens=50)
    ledger.record_from(client_a)
    ledger.record_from(client_a)

    assert ledger.usage.input_tokens == 200
    assert ledger.usage.output_tokens == 100
    assert ledger.usage.total_tokens == 300


def test_token_ledger_ignores_missing_last_usage():
    ledger = TokenLedger()

    class _Bare:
        pass

    ledger.record_from(_Bare())
    assert ledger.usage.total_tokens == 0


def test_estimate_cost_known_model():
    # Opus pricing: $15/MTok in, $75/MTok out.
    cost = estimate_cost_usd("claude-opus-4-6", 1_000_000, 1_000_000)
    assert cost == pytest.approx(90.0, rel=1e-6)


def test_estimate_cost_haiku_family_match():
    # Haiku pricing: $1/MTok in, $5/MTok out. Family prefix match.
    cost = estimate_cost_usd("claude-haiku-4-5-20251001", 2_000_000, 500_000)
    assert cost == pytest.approx(2.0 + 2.5, rel=1e-6)


def test_estimate_cost_unknown_model_returns_zero():
    assert estimate_cost_usd("gpt-unknown", 1_000_000, 1_000_000) == 0.0


@pytest.mark.asyncio
async def test_extractor_accumulates_tokens_into_ledger():
    from engine.app.pipeline.extractor import ClaimExtractor

    fake = _UsageReportingClient(
        responses=[json.dumps({"claims": [{"id": "c1", "text": "x", "type": "factual"}]})],
        per_call_usage=TokenUsage(input_tokens=42, output_tokens=11),
    )
    ledger = TokenLedger()
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=128, ledger=ledger)

    await extractor.extract("some text")

    assert ledger.usage.input_tokens == 42
    assert ledger.usage.output_tokens == 11


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


@pytest.mark.asyncio
async def test_orchestrator_emits_zero_cost_when_stages_are_stubbed():
    """When a caller injects stub components, no Anthropic calls happen and
    the metadata should reflect zero tokens/cost — not stale data from a
    previous run."""
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
    report = await pipeline.run(VerifyRequest(source_context="s", llm_output="o"))

    assert report.metadata.input_tokens == 0
    assert report.metadata.output_tokens == 0
    assert report.metadata.total_tokens == 0
    assert report.metadata.estimated_cost_usd == 0.0
