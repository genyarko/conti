from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from engine.app.models.schemas import Claim, ConsistencyVerdict
from engine.app.prompts.consistency_prompt import (
    CONSISTENCY_BATCH_RESPONSE_SCHEMA,
    CONSISTENCY_BATCH_SYSTEM_PROMPT,
    CONSISTENCY_RESPONSE_SCHEMA,
    CONSISTENCY_SYSTEM_PROMPT,
    CONTRADICTION_RESPONSE_SCHEMA,
    CONTRADICTION_SYSTEM_PROMPT,
    build_consistency_batch_user_prompt,
    build_consistency_user_prompt,
    build_contradiction_user_prompt,
)
from engine.app.services.anthropic_client import (
    AnthropicClient,
    LLMClient as _LLMClient,
    TokenLedger,
)
from engine.app.services import llm_factory
from engine.config import settings

_ClaudeClient = _LLMClient

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
        client: Optional[_LLMClient] = None,
        *,
        model: Optional[str] = None,
        fast_model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        ledger: Optional[TokenLedger] = None,
    ) -> None:
        if client is None:
            resolved = llm_factory.resolve()
            self._client = llm_factory.get_client(resolved.provider)
            self._model = model or resolved.model
            self._fast_model = fast_model or resolved.fast_model
        else:
            self._client = client
            self._model = model or settings.anthropic_model
            self._fast_model = fast_model or settings.anthropic_fast_model
        
        # We use the flagship model's limit for max_tokens if not specified,
        # but individual calls will use llm_factory.max_tokens_for() if needed.
        self._max_tokens = max_tokens or llm_factory.max_tokens_for(self._model)
        self._ledger = ledger

    async def check_source(
        self, claim: Claim, source_context: str
    ) -> tuple[ConsistencyVerdict, int, str]:
        # Source checks are relatively simple -> use the fast model.
        raw = await self._client.create_message(
            system=CONSISTENCY_SYSTEM_PROMPT,
            user=build_consistency_user_prompt(claim.text, source_context),
            model=self._fast_model,
            max_tokens=llm_factory.max_tokens_for(self._fast_model),
            response_schema=CONSISTENCY_RESPONSE_SCHEMA,
        )
        if self._ledger is not None:
            self._ledger.record_from(self._client)
        return _parse_consistency_response(raw)

    async def find_contradictions(
        self, claims: list[Claim]
    ) -> list[ContradictionPair]:
        if len(claims) < 2:
            return []
        # Internal contradictions require higher reasoning -> use the flagship model.
        raw = await self._client.create_message(
            system=CONTRADICTION_SYSTEM_PROMPT,
            user=build_contradiction_user_prompt(
                (c.id, c.text) for c in claims
            ),
            model=self._model,
            max_tokens=self._max_tokens,
            response_schema=CONTRADICTION_RESPONSE_SCHEMA,
        )
        if self._ledger is not None:
            self._ledger.record_from(self._client)
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

        # 1. Source consistency checks.
        source_results_map: dict[str, tuple[ConsistencyVerdict, int, str]] = {}
        
        if len(claims) == 1:
            # A single claim cannot contradict anything, so skip the
            # contradiction sweep entirely.
            verdict, confidence, reasoning = await self.check_source(
                claims[0], source_context
            )
            source_results_map[claims[0].id] = (verdict, confidence, reasoning)
            contradicts_by_id = {}
        else:
            # Batch multiple claims.
            batch_size = settings.pipeline_batch_size
            batches = [claims[i : i + batch_size] for i in range(0, len(claims), batch_size)]
            
            async def run_batch(batch: list[Claim]):
                # Batched source checks are also simple -> use the fast model.
                raw = await self._client.create_message(
                    system=CONSISTENCY_BATCH_SYSTEM_PROMPT,
                    user=build_consistency_batch_user_prompt(
                        [(c.id, c.text) for c in batch], source_context
                    ),
                    model=self._fast_model,
                    max_tokens=llm_factory.max_tokens_for(self._fast_model),
                    response_schema=CONSISTENCY_BATCH_RESPONSE_SCHEMA,
                )
                if self._ledger is not None:
                    self._ledger.record_from(self._client)
                return _parse_json_object(raw)

            # We create tasks in order: source batches first, then contradiction.
            # This ensures FakeClient (which is sequential) sees the expected call order.
            source_tasks = [asyncio.create_task(run_batch(b)) for b in batches]
            contradiction_task = asyncio.create_task(self.find_contradictions(claims))

            batch_responses = await asyncio.gather(*source_tasks)
            
            for i, data in enumerate(batch_responses):
                batch = batches[i]
                batch_results = data.get("results", [])
                results_map = {r.get("claim_id"): r for r in batch_results if r.get("claim_id")}
                
                for claim in batch:
                    r = results_map.get(claim.id)
                    if r:
                        source_results_map[claim.id] = (
                            _coerce_verdict(r.get("verdict")),
                            _coerce_confidence(r.get("confidence", 5)),
                            str(r.get("reasoning") or "").strip()
                        )
                    else:
                        # Retry missing claim individually.
                        log.warning("Batch consistency check omitted claim %s, retrying individually", claim.id)
                        source_results_map[claim.id] = await self.check_source(claim, source_context)
            
            contradictions = await contradiction_task
            contradicts_by_id = _contradicts_index(contradictions)

        # 3. Assemble final results.
        results: list[ConsistencyResult] = []
        for claim in claims:
            verdict, confidence, reasoning = source_results_map.get(
                claim.id, 
                (ConsistencyVerdict.INCONSISTENT, 5, "Batch check failed to return a result.")
            )
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
