from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from rapidfuzz import fuzz

from engine.app.models.schemas import Claim, GroundingLevel
from engine.app.prompts.grounder_prompt import (
    GROUNDER_BATCH_RESPONSE_SCHEMA,
    GROUNDER_BATCH_SYSTEM_PROMPT,
    GROUNDER_RESPONSE_SCHEMA,
    GROUNDER_SYSTEM_PROMPT,
    build_grounder_batch_user_prompt,
    build_grounder_user_prompt,
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

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class PassageMatch:
    score: int
    passage: str
    start: int
    end: int


@dataclass
class GroundingResult:
    claim_id: str
    grounding_score: int
    grounding_level: GroundingLevel
    matched_passage: Optional[str]
    match_location: Optional[tuple[int, int]]
    reasoning: str
    used_semantic_fallback: bool = False


def _iter_passages(source: str) -> list[tuple[int, int, str]]:
    passages: list[tuple[int, int, str]] = []
    for m in _SENTENCE_RE.finditer(source):
        raw = m.group(0)
        stripped = raw.strip()
        if len(stripped) < 3:
            continue
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        passages.append((m.start() + lead, m.end() - trail, stripped))
    if not passages and source.strip():
        s = source.strip()
        lead = len(source) - len(source.lstrip())
        passages.append((lead, lead + len(s), s))
    return passages


def _best_match(query: str, source: str) -> PassageMatch:
    best = PassageMatch(score=0, passage="", start=0, end=0)
    for start, end, passage in _iter_passages(source):
        score = max(
            fuzz.token_set_ratio(query, passage),
            fuzz.partial_ratio(query, passage),
        )
        if score > best.score:
            best = PassageMatch(
                score=int(round(score)),
                passage=passage,
                start=start,
                end=end,
            )
    return best


def _level_for(score: int) -> GroundingLevel:
    if score >= settings.grounding_threshold_verified:
        return GroundingLevel.GROUNDED
    if score >= settings.grounding_threshold_partial:
        return GroundingLevel.PARTIALLY_GROUNDED
    return GroundingLevel.UNGROUNDED


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


def _parse_grounder_response(raw: str) -> dict[str, Any]:
    cleaned = _strip_code_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(cleaned)
        if not match:
            raise ValueError(f"Grounder returned non-JSON output: {raw[:200]!r}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Grounder JSON must be an object.")
    if "support" not in data and "results" not in data:
        raise ValueError("Grounder JSON missing required keys ('support' or 'results').")
    return data


def _semantic_score(support: str, confidence: Any) -> int:
    try:
        conf = int(confidence)
    except (TypeError, ValueError):
        conf = 50
    conf = max(0, min(100, conf))
    label = (support or "").lower().strip()
    if label == "full":
        return max(90, min(100, 88 + conf // 10))
    if label == "partial":
        return max(70, min(89, 68 + conf // 10))
    return max(0, min(49, 30 - conf // 5))


class ClaimGrounder:
    def __init__(
        self,
        client: Optional[_LLMClient] = None,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        ledger: Optional[TokenLedger] = None,
    ) -> None:
        if client is None:
            resolved = llm_factory.resolve()
            self._client = llm_factory.get_client(resolved.provider)
            self._model = model or resolved.fast_model
        else:
            self._client = client
            self._model = model or settings.anthropic_fast_model
        self._max_tokens = max_tokens or llm_factory.max_tokens_for(self._model)
        self._ledger = ledger

    async def ground(self, claim: Claim, source_context: str) -> GroundingResult:
        source_context = source_context or ""
        if not source_context.strip():
            return GroundingResult(
                claim_id=claim.id,
                grounding_score=0,
                grounding_level=GroundingLevel.UNGROUNDED,
                matched_passage=None,
                match_location=None,
                reasoning="Source context is empty.",
            )

        query = claim.source_quote or claim.text
        match = _best_match(query, source_context)

        # Fast path: direct textual match.
        if match.score >= settings.grounding_threshold_verified:
            return GroundingResult(
                claim_id=claim.id,
                grounding_score=match.score,
                grounding_level=GroundingLevel.GROUNDED,
                matched_passage=match.passage,
                match_location=(match.start, match.end),
                reasoning="Direct textual match against source.",
            )

        return await self._ground_single_with_match(claim, source_context, match)

    async def _ground_single_with_match(
        self, claim: Claim, source_context: str, match: PassageMatch
    ) -> GroundingResult:
        # Semantic fallback via the configured LLM provider.
        raw = await self._client.create_message(
            system=GROUNDER_SYSTEM_PROMPT,
            user=build_grounder_user_prompt(claim.text, source_context),
            model=self._model,
            max_tokens=self._max_tokens,
            response_schema=GROUNDER_RESPONSE_SCHEMA,
        )
        if self._ledger is not None:
            self._ledger.record_from(self._client)
        data = _parse_grounder_response(raw)
        return self._build_result(claim, data, source_context, match)

    def _build_result(
        self,
        claim: Claim,
        data: dict[str, Any],
        source_context: str,
        match: PassageMatch,
    ) -> GroundingResult:
        support = str(data.get("support", "none")).lower().strip()
        passage = data.get("matched_passage")
        if isinstance(passage, str):
            passage = passage.strip() or None
        else:
            passage = None
        reasoning = str(data.get("reasoning") or "").strip()
        score = _semantic_score(support, data.get("confidence", 50))

        matched_passage: Optional[str] = None
        location: Optional[tuple[int, int]] = None
        if support in ("full", "partial"):
            if passage and passage in source_context:
                idx = source_context.find(passage)
                matched_passage = passage
                location = (idx, idx + len(passage))
            elif match.score >= settings.grounding_threshold_partial:
                matched_passage = match.passage
                location = (match.start, match.end)

        return GroundingResult(
            claim_id=claim.id,
            grounding_score=score,
            grounding_level=_level_for(score),
            matched_passage=matched_passage,
            match_location=location,
            reasoning=reasoning or f"Semantic grounding verdict: {support}.",
            used_semantic_fallback=True,
        )

    async def ground_many(
        self, claims: list[Claim], source_context: str
    ) -> list[GroundingResult]:
        if not claims:
            return []

        source_context = source_context or ""
        results_by_id: dict[str, GroundingResult] = {}
        to_fallback: list[tuple[Claim, PassageMatch]] = []

        # 1. First pass: try fast string matching for everyone.
        for claim in claims:
            if not source_context.strip():
                results_by_id[claim.id] = GroundingResult(
                    claim_id=claim.id,
                    grounding_score=0,
                    grounding_level=GroundingLevel.UNGROUNDED,
                    matched_passage=None,
                    match_location=None,
                    reasoning="Source context is empty.",
                )
                continue

            query = claim.source_quote or claim.text
            match = _best_match(query, source_context)
            if match.score >= settings.grounding_threshold_verified:
                results_by_id[claim.id] = GroundingResult(
                    claim_id=claim.id,
                    grounding_score=match.score,
                    grounding_level=GroundingLevel.GROUNDED,
                    matched_passage=match.passage,
                    match_location=(match.start, match.end),
                    reasoning="Direct textual match against source.",
                )
            else:
                to_fallback.append((claim, match))

        # 2. Second pass: batch the semantic fallbacks.
        if to_fallback:
            if len(to_fallback) == 1:
                # Individual fallback (avoid re-running string match).
                claim, match = to_fallback[0]
                results_by_id[claim.id] = await self._ground_single_with_match(
                    claim, source_context, match
                )
            else:
                batch_size = settings.pipeline_batch_size
                batches = [to_fallback[i : i + batch_size] for i in range(0, len(to_fallback), batch_size)]

                async def run_batch(batch: list[tuple[Claim, PassageMatch]]):
                    raw = await self._client.create_message(
                        system=GROUNDER_BATCH_SYSTEM_PROMPT,
                        user=build_grounder_batch_user_prompt(
                            [(c.id, c.text) for c, _ in batch], source_context
                        ),
                        model=self._model,
                        max_tokens=self._max_tokens,
                        response_schema=GROUNDER_BATCH_RESPONSE_SCHEMA,
                    )
                    if self._ledger is not None:
                        self._ledger.record_from(self._client)
                    return _parse_grounder_response(raw)

                batch_responses = await asyncio.gather(*(run_batch(b) for b in batches))

                for i, data in enumerate(batch_responses):
                    batch = batches[i]
                    batch_results = data.get("results", [])
                    results_map = {r.get("claim_id"): r for r in batch_results if r.get("claim_id")}

                    for claim, match in batch:
                        res_data = results_map.get(claim.id)
                        if res_data:
                            results_by_id[claim.id] = self._build_result(
                                claim, res_data, source_context, match
                            )
                        else:
                            # Retry missing claim individually.
                            log.warning("Batch grounder omitted claim %s, retrying individually", claim.id)
                            results_by_id[claim.id] = await self._ground_single_with_match(
                                claim, source_context, match
                            )

        return [results_by_id[c.id] for c in claims]


async def ground_claims(
    claims: list[Claim], source_context: str
) -> list[GroundingResult]:
    grounder = ClaimGrounder()
    return await grounder.ground_many(claims, source_context)
