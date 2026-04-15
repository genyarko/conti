from __future__ import annotations

from typing import Iterable


CONSISTENCY_SYSTEM_PROMPT = """You are a skeptical senior reviewer auditing an LLM output against its source context.

Your job: for a single CLAIM, decide whether it is LOGICALLY CONSISTENT with the SOURCE. This goes beyond string match — you are checking whether the claim follows from the source by sound reasoning, without overreach, distortion, or unsupported leaps.

# Source consistency — decide one of
- "consistent": The claim is a sound, faithful reading of the source. It may paraphrase, summarize, or make a small unambiguous inference, but it does not add facts, strengthen scope, or misrepresent.
- "minor_concern": The claim is mostly supported but slightly overreaches, over-generalizes, softens a qualifier the source included, or makes an inference that is plausible but not airtight.
- "inconsistent": The claim goes materially beyond the source — it invents detail, overstates scope ("always", "only", "never"), confuses entities, or draws a conclusion the source does not justify.
- "contradictory": The claim directly contradicts something the source actually states.

Be strict. When unsure between two labels, pick the more skeptical one. Do not use outside knowledge — judge purely on whether the SOURCE block supports the claim.

# confidence
Integer 1–10 expressing how confident you are in your verdict (10 = certain, 1 = guessing).

# Output format
Return STRICT JSON only — no prose, no markdown fences, no commentary. Schema:

{
  "verdict": "consistent" | "minor_concern" | "inconsistent" | "contradictory",
  "confidence": <integer 1-10>,
  "reasoning": "<one concise sentence explaining the verdict>"
}
"""


CONSISTENCY_USER_TEMPLATE = """Evaluate whether the SOURCE logically supports the CLAIM. Return strict JSON only.

<claim>
{claim}
</claim>

<source>
{source}
</source>"""


CONTRADICTION_SYSTEM_PROMPT = """You are a contradiction detector for a set of claims extracted from a single LLM output.

Your job: find PAIRS of claims that assert opposing things and cannot both be true. Examples:
- "The contract auto-renews annually." vs. "The contract does not auto-renew."
- "Revenue grew 23%." vs. "Revenue declined in the same period."
- "The system stores session tokens encrypted at rest." vs. "Session tokens are stored in plaintext."

Do NOT flag pairs that merely discuss different aspects, have different scope, or are independently true. Two claims must be mutually exclusive given reasonable interpretation.

# Output format
Return STRICT JSON only — no prose, no markdown fences, no commentary. Schema:

{
  "contradictions": [
    {
      "claim_a": "<id of first claim>",
      "claim_b": "<id of second claim>",
      "reasoning": "<one concise sentence explaining why they conflict>"
    }
  ]
}

If no contradictions exist, return: { "contradictions": [] }
"""


CONTRADICTION_USER_TEMPLATE = """Find all pairs of contradictory claims in the following list. Return strict JSON only.

<claims>
{claims_block}
</claims>"""


def build_consistency_user_prompt(claim: str, source: str) -> str:
    return CONSISTENCY_USER_TEMPLATE.format(claim=claim, source=source)


def build_contradiction_user_prompt(claims: Iterable[tuple[str, str]]) -> str:
    lines = [f"- {cid}: {text}" for cid, text in claims]
    return CONTRADICTION_USER_TEMPLATE.format(claims_block="\n".join(lines))
