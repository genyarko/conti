from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import pytest

from engine.app.models.schemas import ClaimCategory
from engine.app.pipeline.extractor import (
    CHUNK_CHAR_LIMIT,
    ClaimExtractor,
    _chunk_output,
    _looks_like_structured,
    _parse_claims_payload,
)


@dataclass
class FakeClient:
    """Returns canned responses in order; records all calls."""

    responses: list[str]
    calls: list[dict] = field(default_factory=list)
    _idx: int = 0

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
    ) -> str:
        self.calls.append(
            {"system": system, "user": user, "model": model, "max_tokens": max_tokens}
        )
        if self._idx >= len(self.responses):
            raise AssertionError("FakeClient ran out of canned responses")
        out = self.responses[self._idx]
        self._idx += 1
        return out


def _resp(claims: list[dict]) -> str:
    return json.dumps({"claims": claims})


# ---------- pure helpers ----------


def test_looks_like_structured_detects_json_object():
    assert _looks_like_structured('{"a": 1, "b": 2}')


def test_looks_like_structured_detects_json_array():
    assert _looks_like_structured("[1, 2, 3]")


def test_looks_like_structured_rejects_prose():
    assert not _looks_like_structured("The capital of France is Paris.")


def test_parse_claims_payload_strips_code_fences():
    raw = '```json\n{"claims": [{"id": "c1", "text": "x", "type": "factual"}]}\n```'
    out = _parse_claims_payload(raw)
    assert out[0]["text"] == "x"


def test_parse_claims_payload_recovers_embedded_json():
    raw = 'Sure! Here it is: {"claims": []} hope that helps'
    assert _parse_claims_payload(raw) == []


def test_parse_claims_payload_raises_on_missing_key():
    with pytest.raises(ValueError):
        _parse_claims_payload('{"foo": 1}')


def test_chunk_output_short_input_single_chunk():
    assert _chunk_output("hello") == ["hello"]


def test_chunk_output_long_input_splits():
    text = "para one.\n\n" + ("a" * (CHUNK_CHAR_LIMIT)) + "\n\npara end."
    chunks = _chunk_output(text)
    assert len(chunks) >= 2
    assert "".join(chunks).count("para end.") >= 1


# ---------- extractor behavior ----------


@pytest.mark.asyncio
async def test_factual_claim_extraction():
    fake = FakeClient(
        responses=[
            _resp(
                [
                    {
                        "id": "c1",
                        "text": "Paris is the capital of France.",
                        "type": "factual",
                        "source_quote_if_any": "Paris is the capital of France.",
                    }
                ]
            )
        ]
    )
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract("Paris is the capital of France.")
    assert len(result.claims) == 1
    assert result.claims[0].category == ClaimCategory.FACTUAL
    assert result.claims[0].output_quote == "Paris is the capital of France."


@pytest.mark.asyncio
async def test_multi_claim_with_categories():
    output = "Revenue grew 23% in Q3. The team should hire two more engineers. This is the best quarter ever."
    fake = FakeClient(
        responses=[
            _resp(
                [
                    {
                        "id": "c1",
                        "text": "Revenue grew 23% in Q3.",
                        "type": "quantitative",
                        "source_quote_if_any": "Revenue grew 23% in Q3.",
                    },
                    {
                        "id": "c2",
                        "text": "The team should hire two more engineers.",
                        "type": "recommendation",
                        "source_quote_if_any": "The team should hire two more engineers.",
                    },
                    {
                        "id": "c3",
                        "text": "This is the best quarter ever.",
                        "type": "interpretive",
                        "source_quote_if_any": "This is the best quarter ever.",
                    },
                ]
            )
        ]
    )
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract(output)
    cats = [c.category for c in result.claims]
    assert cats == [
        ClaimCategory.QUANTITATIVE,
        ClaimCategory.RECOMMENDATION,
        ClaimCategory.INTERPRETIVE,
    ]


@pytest.mark.asyncio
async def test_no_verifiable_claims_returns_empty():
    fake = FakeClient(responses=[_resp([])])
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract("Hello! Sure, what can I help with?")
    assert result.claims == []


@pytest.mark.asyncio
async def test_empty_input_short_circuits_without_call():
    fake = FakeClient(responses=[])
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract("   \n  ")
    assert result.claims == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_structured_input_uses_structured_prompt():
    payload = '{"city": "Paris", "population": 2100000}'
    fake = FakeClient(
        responses=[
            _resp(
                [
                    {
                        "id": "c1",
                        "text": "city is Paris.",
                        "type": "factual",
                        "source_quote_if_any": '"city": "Paris"',
                    },
                    {
                        "id": "c2",
                        "text": "population is 2100000.",
                        "type": "quantitative",
                        "source_quote_if_any": '"population": 2100000',
                    },
                ]
            )
        ]
    )
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract(payload)
    assert len(result.claims) == 2
    assert "structured data" in fake.calls[0]["user"]


@pytest.mark.asyncio
async def test_long_output_is_chunked_into_multiple_calls():
    long_text = ("First paragraph about apples.\n\n" * 200) + (
        "a" * CHUNK_CHAR_LIMIT
    ) + "\n\nFinal paragraph about oranges."
    # Pre-compute how many chunks the splitter will produce so we can supply
    # one canned response per chunk.
    from engine.app.pipeline.extractor import _chunk_output

    chunk_count = len(_chunk_output(long_text))
    responses = [
        _resp(
            [{"id": "c1", "text": "First paragraph about apples.", "type": "factual"}]
        )
    ] + [_resp([]) for _ in range(chunk_count - 2)] + [
        _resp(
            [{"id": "c1", "text": "Final paragraph about oranges.", "type": "factual"}]
        )
    ]
    fake = FakeClient(responses=responses)
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract(long_text)
    assert len(fake.calls) == chunk_count
    assert chunk_count >= 2
    texts = {c.text for c in result.claims}
    assert "First paragraph about apples." in texts
    assert "Final paragraph about oranges." in texts


@pytest.mark.asyncio
async def test_duplicate_claims_are_deduplicated():
    fake = FakeClient(
        responses=[
            _resp(
                [
                    {"id": "c1", "text": "The sky is blue.", "type": "factual"},
                    {"id": "c2", "text": "the sky is blue.", "type": "factual"},
                ]
            )
        ]
    )
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract("The sky is blue. The sky is blue.")
    assert len(result.claims) == 1


@pytest.mark.asyncio
async def test_quote_not_in_output_is_dropped():
    fake = FakeClient(
        responses=[
            _resp(
                [
                    {
                        "id": "c1",
                        "text": "Paris is the capital of France.",
                        "type": "factual",
                        "source_quote_if_any": "totally fabricated quote",
                    }
                ]
            )
        ]
    )
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract("Paris is the capital of France.")
    assert result.claims[0].output_quote is None


@pytest.mark.asyncio
async def test_unknown_category_falls_back_to_factual():
    fake = FakeClient(
        responses=[
            _resp([{"id": "c1", "text": "Some claim.", "type": "speculative"}])
        ]
    )
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    result = await extractor.extract("Some claim.")
    assert result.claims[0].category == ClaimCategory.FACTUAL


@pytest.mark.asyncio
async def test_malformed_response_raises():
    fake = FakeClient(responses=["not json at all"])
    extractor = ClaimExtractor(client=fake, model="m", max_tokens=512)
    with pytest.raises(ValueError):
        await extractor.extract("anything")
