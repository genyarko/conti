from __future__ import annotations

from typing import Any

# Response schema for the grounder. Sent to Gemini via `response_schema`;
# mirrored in prose form inside the system prompt for Anthropic models.
GROUNDER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "support": {
            "type": "string",
            "enum": ["full", "partial", "none"],
        },
        "matched_passage": {
            "type": "string",
            "description": (
                "Shortest verbatim slice of the source that supports the "
                "claim. Empty string when support is 'none'."
            ),
        },
        "confidence": {
            "type": "integer",
            "description": "0–100 confidence in the support verdict.",
        },
        "reasoning": {
            "type": "string",
            "description": "One concise sentence justifying the verdict.",
        },
    },
    "required": ["support", "confidence", "reasoning"],
}

# Batched version for multiple claims in one request.
GROUNDER_BATCH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "support": {"type": "string", "enum": ["full", "partial", "none"]},
                    "matched_passage": {"type": "string"},
                    "confidence": {"type": "integer"},
                    "reasoning": {"type": "string"},
                },
                "required": ["claim_id", "support", "confidence", "reasoning"],
            },
        }
    },
    "required": ["results"],
}


GROUNDER_SYSTEM_PROMPT = """You are a strict grounding verifier for an LLM-output verification pipeline.

Given a single CLAIM and a SOURCE text, decide whether the source supports the claim — either explicitly or through minimal, unambiguous inference.

# Support levels
- "full": The source explicitly states, paraphrases, or directly entails every factual component of the claim. No leap required.
- "partial": The source mentions the topic or closely related facts but does not fully cover the specific claim. Covers missing detail, weaker version of the claim, or meaningful inference beyond what is stated.
- "none": The source does not support the claim. The claim is fabricated, contradicted by the source, or about something the source never addresses.

Be strict. When unsure, prefer "partial" over "full" and "none" over "partial". Do not use outside knowledge — only what is in the SOURCE block.

# matched_passage
When support is "full" or "partial", return the SHORTEST verbatim contiguous slice of the SOURCE that best supports the claim. It MUST appear in the source character-for-character. When support is "none", set matched_passage to null.

# confidence
Integer 0–100 expressing how confident you are in the support verdict itself.

# Output format
Return STRICT JSON only — no prose, no markdown fences, no commentary. Schema:

{
  "support": "full" | "partial" | "none",
  "matched_passage": "<verbatim slice of source, or null>",
  "confidence": <integer 0-100>,
  "reasoning": "<one concise sentence>"
}
"""

GROUNDER_BATCH_SYSTEM_PROMPT = """You are a strict grounding verifier for an LLM-output verification pipeline.

Given a list of CLAIMS and a SOURCE text, decide for EACH claim whether the source supports it — either explicitly or through minimal, unambiguous inference.

# Support levels
- "full": The source explicitly states, paraphrases, or directly entails every factual component of the claim. No leap required.
- "partial": The source mentions the topic or closely related facts but does not fully cover the specific claim. Covers missing detail, weaker version of the claim, or meaningful inference beyond what is stated.
- "none": The source does not support the claim. The claim is fabricated, contradicted by the source, or about something the source never addresses.

Be strict. When unsure, prefer "partial" over "full" and "none" over "partial". Do not use outside knowledge — only what is in the SOURCE block.

# matched_passage
When support is "full" or "partial", return the SHORTEST verbatim contiguous slice of the SOURCE that best supports the claim. It MUST appear in the source character-for-character. When support is "none", set matched_passage to null.

# Output format
Return STRICT JSON only — no prose, no markdown fences, no commentary. The "results" array MUST contain one entry per claim, using the exact claim_id provided. Schema:

{
  "results": [
    {
      "claim_id": "string",
      "support": "full" | "partial" | "none",
      "matched_passage": "string or null",
      "confidence": integer 0-100,
      "reasoning": "string"
    }
  ]
}
"""


GROUNDER_USER_TEMPLATE = """Decide whether the SOURCE supports the CLAIM. Return strict JSON only.

<claim>
{claim}
</claim>

<source>
{source}
</source>"""

GROUNDER_BATCH_USER_TEMPLATE = """Decide whether the SOURCE supports each of the following CLAIMS. Return strict JSON only.

<claims>
{claims_block}
</claims>

<source>
{source}
</source>"""


def build_grounder_user_prompt(claim: str, source: str) -> str:
    return GROUNDER_USER_TEMPLATE.format(claim=claim, source=source)


def build_grounder_batch_user_prompt(claims: list[tuple[str, str]], source: str) -> str:
    lines = [f"- {cid}: {text}" for cid, text in claims]
    return GROUNDER_BATCH_USER_TEMPLATE.format(
        claims_block="\n".join(lines), source=source
    )
