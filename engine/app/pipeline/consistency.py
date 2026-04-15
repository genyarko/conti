from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from engine.app.models.schemas import Claim, ConsistencyVerdict
from engine.app.pipeline.extractor import AnthropicClient
from engine.app.prompts.consistency_prompt import (
    CONSISTENCY_SYSTEM_PROMPT,
    CONTRADICTION_SYSTEM_PROMPT,
    build_consistency_user_prompt,
    build_contradiction_user_prompt,
)
from engine.config import settings

log = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_VERDICT_ALIASES = {
    "consistent": ConsistencyVerdict.CONSISTENT,
    "minor_concern": ConsistencyVerdict.MINOR_CONCERN,
    "minor-concern": ConsistencyVerdict.MINOR_CONCERN,
    "minor": ConsistencyVerdict.MINOR_CONCERN,
    "inconsistent": ConsistencyVerdict.INCONSISTENT,
    "contradictory": ConsistencyVerdict.CONTRADICTORY,
    "contradiction": ConsistencyVerdict.CONTRADICTORY,
}

_SOURCE_CONSISTENT_VERDICTS = frozenset(
    {ConsistencyVerdict.CONSISTENT, ConsistencyVerdict.MINOR_CONCERN}
)


class _ClaudeClient(Protocol):
    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
    ) -> str: ...


@dataclass
class ContradictionPair:
    claim_a: str
    claim_b: str
    reasoning: str


@dataclass
class ConsistencyResult:
    claim_id: str
    verdict: ConsistencyVerdict
    source_consistent: bool
    internal_consistent: bool
    confidence: int
    reasoning: str
    contradicts: list[str] = field(default_factory=list)


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


def _parse_json_object(raw: str) -> dict[str, Any]:
    cleaned = _strip_code_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(cleaned)
        if not match:
            raise ValueError(f"Consistency checker returned non-JSON output: {raw[:200]!r}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Consistency checker JSON must be an object.")
    return data


def _coerce_verdict(value: Any) -> ConsistencyVerdict:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _VERDICT_ALIASES:
            return _VERDICT_ALIASES[key]
    # Default to the most skeptical neutral label when the model misbehaves.
    return ConsistencyVerdict.INCONSISTENT


def _coerce_confidence(value: Any) -> int:
    try:
        conf = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(10, conf))


def _parse_consistency_response(raw: str) -> tuple[ConsistencyVerdict, int, str]:
    data = _parse_json_object(raw)
    if "verdict" not in data:
        raise ValueError("Consistency JSON missing 'verdict' key.")
    verdict = _coerce_verdict(data.get("verdict"))
    confidence = _coerce_confidence(data.get("confidence", 5))
    reasoning = str(data.get("reasoning") or "").strip()
    return verdict, confidence, reasoning


def _parse_contradictions_response(
    raw: str, valid_ids: set[str]
) -> list[ContradictionPair]:
    data = _parse_json_object(raw)
    if "contradictions" not in data:
        raise ValueError("Contradiction JSON missing 'contradictions' key.")
    items = data["contradictions"]
    if not isinstance(items, list):
        raise ValueError("'contradictions' field must be a list.")
    pairs: list[ContradictionPair] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        a = str(item.get("claim_a") or "").strip()
        b = str(item.get("claim_b") or "").strip()
        if not a or not b or a == b:
            continue
        if a not in valid_ids or b not in valid_ids:
            continue
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            ContradictionPair(
                claim_a=a,
                claim_b=b,
                reasoning=str(item.get("reasoning") or "").strip(),
            )
        )
    return pairs


class ConsistencyChecker:
    def __init__(
        self,
        client: Optional[_ClaudeClient] = None,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        self._client = client or AnthropicClient(api_key=settings.anthropic_api_key)
        self._model = model or settings.anthropic_fast_model
        self._max_tokens = max_tokens or settings.anthropic_max_tokens

    async def check_source(
        self, claim: Claim, source_context: str
    ) -> tuple[ConsistencyVerdict, int, str]:
        raw = await self._client.create_message(
            system=CONSISTENCY_SYSTEM_PROMPT,
            user=build_consistency_user_prompt(claim.text, source_context),
            model=self._model,
            max_tokens=self._max_tokens,
        )
        return _parse_consistency_response(raw)

    async def find_contradictions(
        self, claims: list[Claim]
    ) -> list[ContradictionPair]:
        if len(claims) < 2:
            return []
        raw = await self._client.create_message(
            system=CONTRADICTION_SYSTEM_PROMPT,
            user=build_contradiction_user_prompt(
                (c.id, c.text) for c in claims
            ),
            model=self._model,
            max_tokens=self._max_tokens,
        )
        valid_ids = {c.id for c in claims}
        return _parse_contradictions_response(raw, valid_ids)

    async def check(
        self, claims: list[Claim], source_context: str
    ) -> list[ConsistencyResult]:
        if not claims:
            return []

        source_context = source_context or ""
        if not source_context.strip():
            # Without a source, no claim can be judged source-consistent.
            contradictions = await self.find_contradictions(claims)
            contradicts_by_id = _contradicts_index(contradictions)
            return [
                ConsistencyResult(
                    claim_id=c.id,
                    verdict=ConsistencyVerdict.INCONSISTENT,
                    source_consistent=False,
                    internal_consistent=c.id not in contradicts_by_id,
                    confidence=10,
                    reasoning="Source context is empty.",
                    contradicts=sorted(contradicts_by_id.get(c.id, set())),
                )
                for c in claims
            ]

        source_task = asyncio.gather(
            *(self.check_source(c, source_context) for c in claims)
        )
        contradiction_task = asyncio.create_task(self.find_contradictions(claims))
        source_results, contradictions = await asyncio.gather(
            source_task, contradiction_task
        )

        contradicts_by_id = _contradicts_index(contradictions)
        results: list[ConsistencyResult] = []
        for claim, (verdict, confidence, reasoning) in zip(claims, source_results):
            conflicts = contradicts_by_id.get(claim.id, set())
            internal_ok = len(conflicts) == 0
            # A claim participating in a contradiction escalates to contradictory.
            if not internal_ok and verdict in _SOURCE_CONSISTENT_VERDICTS:
                verdict = ConsistencyVerdict.CONTRADICTORY
            results.append(
                ConsistencyResult(
                    claim_id=claim.id,
                    verdict=verdict,
                    source_consistent=verdict in _SOURCE_CONSISTENT_VERDICTS,
                    internal_consistent=internal_ok,
                    confidence=confidence,
                    reasoning=reasoning or f"Verdict: {verdict.value}.",
                    contradicts=sorted(conflicts),
                )
            )
        return results


def _contradicts_index(pairs: list[ContradictionPair]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for p in pairs:
        index.setdefault(p.claim_a, set()).add(p.claim_b)
        index.setdefault(p.claim_b, set()).add(p.claim_a)
    return index


async def check_consistency(
    claims: list[Claim], source_context: str
) -> list[ConsistencyResult]:
    checker = ConsistencyChecker()
    return await checker.check(claims, source_context)
