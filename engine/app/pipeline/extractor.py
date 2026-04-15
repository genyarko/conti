from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from anthropic import AsyncAnthropic

from engine.app.models.schemas import Claim, ClaimCategory
from engine.app.prompts.extractor_prompt import (
    EXTRACTOR_SYSTEM_PROMPT,
    build_user_prompt,
)
from engine.config import settings

log = logging.getLogger(__name__)

# Heuristic char-budget per chunk. ~4 chars/token, leaving room for prompt + response.
CHUNK_CHAR_LIMIT = 12000
# Below this, treat as a single chunk regardless.
CHUNK_OVERLAP = 200

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
class AnthropicClient:
    """Thin wrapper so tests can substitute a fake without touching the SDK."""

    api_key: str
    _client: Optional[AsyncAnthropic] = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for the extractor.")
        self._client = AsyncAnthropic(api_key=self.api_key)

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
    ) -> str:
        assert self._client is not None
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Concatenate all text blocks in the response.
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)


def _looks_like_structured(output: str) -> bool:
    s = output.strip()
    if not s:
        return False
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            json.loads(s)
            return True
        except json.JSONDecodeError:
            return False
    return False


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


def _parse_claims_payload(raw: str) -> list[dict[str, Any]]:
    cleaned = _strip_code_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(cleaned)
        if not match:
            raise ValueError(f"Extractor returned non-JSON output: {raw[:200]!r}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict) or "claims" not in data:
        raise ValueError("Extractor JSON missing 'claims' key.")
    claims = data["claims"]
    if not isinstance(claims, list):
        raise ValueError("Extractor 'claims' field must be a list.")
    return claims


def _coerce_category(value: Any) -> ClaimCategory:
    if isinstance(value, str):
        try:
            return ClaimCategory(value.lower().strip())
        except ValueError:
            pass
    return ClaimCategory.FACTUAL


def _to_claim(raw: dict[str, Any], llm_output: str) -> Optional[Claim]:
    text = (raw.get("text") or "").strip()
    if not text:
        return None
    quote = raw.get("source_quote_if_any") or raw.get("output_quote")
    if isinstance(quote, str):
        quote = quote.strip() or None
        # Verify the quote actually appears in the source; drop if hallucinated.
        if quote and quote not in llm_output:
            quote = None
    else:
        quote = None
    return Claim(
        text=text,
        output_quote=quote,
        category=_coerce_category(raw.get("type")),
    )


def _chunk_output(output: str, limit: int = CHUNK_CHAR_LIMIT) -> list[str]:
    if len(output) <= limit:
        return [output]
    chunks: list[str] = []
    start = 0
    while start < len(output):
        end = min(start + limit, len(output))
        # Try to break on a paragraph boundary near the limit.
        if end < len(output):
            window = output.rfind("\n\n", start, end)
            if window > start + limit // 2:
                end = window
        chunks.append(output[start:end])
        if end >= len(output):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


@dataclass
class ExtractionResult:
    claims: list[Claim]
    raw_responses: list[str]


class ClaimExtractor:
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

    async def extract(self, llm_output: str) -> ExtractionResult:
        if not llm_output or not llm_output.strip():
            return ExtractionResult(claims=[], raw_responses=[])

        structured = _looks_like_structured(llm_output)
        chunks = _chunk_output(llm_output)
        log.debug(
            "extractor: %d chunk(s), structured=%s, total_chars=%d",
            len(chunks),
            structured,
            len(llm_output),
        )

        all_claims: list[Claim] = []
        raw_responses: list[str] = []
        seen_text: set[str] = set()

        for chunk in chunks:
            user_prompt = build_user_prompt(chunk, structured=structured)
            raw = await self._client.create_message(
                system=EXTRACTOR_SYSTEM_PROMPT,
                user=user_prompt,
                model=self._model,
                max_tokens=self._max_tokens,
            )
            raw_responses.append(raw)
            payload = _parse_claims_payload(raw)
            for item in payload:
                if not isinstance(item, dict):
                    continue
                claim = _to_claim(item, llm_output)
                if claim is None:
                    continue
                key = claim.text.lower()
                if key in seen_text:
                    continue
                seen_text.add(key)
                all_claims.append(claim)

        return ExtractionResult(claims=all_claims, raw_responses=raw_responses)


async def extract_claims(llm_output: str) -> list[Claim]:
    extractor = ClaimExtractor()
    result = await extractor.extract(llm_output)
    return result.claims
