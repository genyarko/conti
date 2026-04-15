from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.models.schemas import (
    Clause,
    ContractSummary,
    Finding,
    FindingCategory,
    RiskLevel,
)
from app.prompts.analyzer_prompt import ANALYZER_SYSTEM_PROMPT, build_analyzer_user_prompt
from app.services.anthropic_client import AnthropicClient, ClaudeClient

log = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class AnalysisResult:
    summary: ContractSummary
    findings: list[Finding]
    missing_clauses: list[Finding]
    raw_response: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ContractAnalyzer:
    def __init__(
        self,
        client: Optional[ClaudeClient] = None,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        self._client = client or AnthropicClient(api_key=settings.anthropic_api_key)
        self._model = model or settings.anthropic_model
        self._max_tokens = max_tokens or settings.anthropic_max_tokens

    async def analyze(
        self,
        clauses: list[Clause],
        *,
        filename: str | None = None,
    ) -> AnalysisResult:
        if not clauses:
            return AnalysisResult(
                summary=ContractSummary(plain_language_summary="Empty contract."),
                findings=[],
                missing_clauses=[],
            )

        payload = [
            {"section_id": c.section_id, "title": c.title, "text": c.text}
            for c in clauses
        ]
        user_prompt = build_analyzer_user_prompt(payload, filename=filename)

        raw = await self._client.create_message(
            system=ANALYZER_SYSTEM_PROMPT,
            user=user_prompt,
            model=self._model,
            max_tokens=self._max_tokens,
        )
        parsed = _parse_response(raw)

        clause_index = {c.section_id: c for c in clauses}
        findings = [
            _to_finding(item, clause_index)
            for item in parsed.get("findings", [])
            if isinstance(item, dict)
        ]
        findings = [f for f in findings if f is not None]

        missing = [
            _to_missing_finding(item)
            for item in parsed.get("missing_clauses", [])
            if isinstance(item, dict)
        ]
        missing = [f for f in missing if f is not None]

        summary = ContractSummary(
            contract_type=str(parsed.get("contract_type") or "Unknown"),
            overall_risk=_coerce_risk(parsed.get("overall_risk"), default=RiskLevel.INFO),
            integrity_score=0,  # populated after verification
            plain_language_summary=str(parsed.get("plain_language_summary") or ""),
            key_parties=[str(p) for p in (parsed.get("parties") or []) if p],
        )

        return AnalysisResult(
            summary=summary,
            findings=findings,
            missing_clauses=missing,
            raw_response=raw,
        )


def _strip_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


def _parse_response(raw: str) -> dict[str, Any]:
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(cleaned)
        if not match:
            raise ValueError(f"Analyzer returned non-JSON output: {raw[:200]!r}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Analyzer JSON root must be an object.")
    return data


def _coerce_risk(value: Any, *, default: RiskLevel) -> RiskLevel:
    if isinstance(value, str):
        try:
            return RiskLevel(value.lower().strip())
        except ValueError:
            pass
    return default


def _coerce_category(value: Any) -> FindingCategory:
    if isinstance(value, str):
        try:
            return FindingCategory(value.lower().strip())
        except ValueError:
            pass
    return FindingCategory.OTHER


def _to_finding(raw: dict[str, Any], clause_index: dict[str, Clause]) -> Optional[Finding]:
    section_id = str(raw.get("section_id") or "").strip()
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    if not section_id or not title or not summary:
        return None

    # Drop the finding if it points at a clause the analyzer hallucinated.
    if section_id != "missing" and section_id not in clause_index:
        log.warning("analyzer: dropping finding pointing at unknown section_id=%s", section_id)
        return None

    quote = raw.get("clause_quote")
    if isinstance(quote, str):
        quote = quote.strip() or None
        # If the quote isn't literally in the referenced clause, strip it —
        # the verifier will still score the claim against the clause text.
        clause = clause_index.get(section_id)
        if quote and clause and quote not in clause.text:
            quote = None
    else:
        quote = None

    return Finding(
        section_id=section_id,
        title=title,
        risk=_coerce_risk(raw.get("risk"), default=RiskLevel.WARNING),
        category=_coerce_category(raw.get("category")),
        summary=summary,
        recommendation=str(raw.get("recommendation") or "").strip(),
        clause_quote=quote,
    )


def _to_missing_finding(raw: dict[str, Any]) -> Optional[Finding]:
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    if not title or not summary:
        return None
    return Finding(
        section_id="missing",
        title=title,
        risk=_coerce_risk(raw.get("risk"), default=RiskLevel.WARNING),
        category=FindingCategory.MISSING_CLAUSE,
        summary=summary,
        recommendation=str(raw.get("recommendation") or "").strip(),
        clause_quote=None,
    )
