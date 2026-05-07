from __future__ import annotations

import json
from dataclasses import dataclass, field
import pytest
from engine.app.models.schemas import Claim, ClaimCategory, GroundingLevel
from engine.app.pipeline.grounder import ClaimGrounder

@dataclass
class FakeClient:
    responses: list[str]
    calls: list[dict] = field(default_factory=list)
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
        return out

def _gresp(support: str, matched_passage: str | None = None, confidence: int = 80, reasoning: str = "ok") -> str:
    return json.dumps({
        "support": support,
        "matched_passage": matched_passage,
        "confidence": confidence,
        "reasoning": reasoning
    })

@pytest.mark.asyncio
async def test_absence_claim_is_grounded_when_actually_missing():
    # If a claim says something is missing, and it IS missing, grounding should be GROUNDED (100).
    client = FakeClient(responses=[_gresp("full", None, 100)])
    grounder = ClaimGrounder(client=client)
    source = "This contract covers services and payment. It does not mention liability."
    claim = Claim(
        text="The contract is missing a Governing Law clause.",
        category=ClaimCategory.MISSING_CLAUSE
    )
    
    result = await grounder.ground(claim, source)
    
    assert result.grounding_level == GroundingLevel.GROUNDED
    assert result.grounding_score >= 90
    assert result.matched_passage is None
    assert "Absence Verifier" in client.calls[0]["system"]

@pytest.mark.asyncio
async def test_absence_claim_is_ungrounded_when_actually_present():
    # If a claim says something is missing, but it IS present, grounding should be UNGROUNDED (0).
    # matched_passage should contain the proof of existence.
    source = "Governing Law: This agreement is governed by the laws of New York."
    verbatim = "governed by the laws of New York"
    client = FakeClient(responses=[_gresp("none", verbatim, 100)])
    grounder = ClaimGrounder(client=client)
    claim = Claim(
        text="The contract is missing a Governing Law clause.",
        category=ClaimCategory.MISSING_CLAUSE
    )
    
    result = await grounder.ground(claim, source)
    
    assert result.grounding_level == GroundingLevel.UNGROUNDED
    assert result.grounding_score < 50
    assert result.matched_passage == verbatim

