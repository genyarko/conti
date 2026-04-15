from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from rapidfuzz import fuzz

from engine.app.models.schemas import Claim, GroundingLevel
from engine.app.pipeline.extractor import AnthropicClient
from engine.app.prompts.grounder_prompt import (
    GROUNDER_SYSTEM_PROMPT,
    build_grounder_user_prompt,
)
from engine.config import settings

log = logging.getLogger(__name__)

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


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
    if not isinstance(data, dict) or "support" not in data:
        raise ValueError("Grounder JSON missing 'support' key.")
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
        client: Optional[_ClaudeClient] = None,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        self._client = client or AnthropicClient(api_key=settings.anthropic_api_key)
        self._model = model or settings.anthropic_fast_model
        self._max_tokens = max_tokens or settings.anthropic_max_tokens

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

        # Semantic fallback via Claude.
        raw = await self._client.create_message(
            system=GROUNDER_SYSTEM_PROMPT,
            user=build_grounder_user_prompt(claim.text, source_context),
            model=self._model,
            max_tokens=self._max_tokens,
        )
        data = _parse_grounder_response(raw)
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
        return await asyncio.gather(
            *(self.ground(c, source_context) for c in claims)
        )


async def ground_claims(
    claims: list[Claim], source_context: str
) -> list[GroundingResult]:
    grounder = ClaimGrounder()
    return await grounder.ground_many(claims, source_context)
