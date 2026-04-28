from __future__ import annotations

import logging
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
from app.prompts.analyzer_prompt import (
    ANALYZER_SYSTEM_PROMPT,
    ANALYZER_TOOL_DESCRIPTION,
    ANALYZER_TOOL_NAME,
    ANALYZER_TOOL_SCHEMA,
    build_analyzer_user_prompt,
)
from app.services import llm_factory
from app.services.anthropic_client import LLMClient

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    summary: ContractSummary
    findings: list[Finding]
    missing_clauses: list[Finding]
    raw_response: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ContractAnalyzer:
    def __init__(
        self,
        client: Optional[LLMClient] = None,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        if client is None:
            resolved = llm_factory.resolve(provider=provider, model=model)
            self._client = llm_factory.get_client(resolved.provider)
            self._provider = resolved.provider
            self._model = model or resolved.model
        else:
            self._client = client
            # Best-effort provider tag for caller-injected clients (used to
            # decide whether multimodal image_parts are accepted).
            self._provider = (
                provider
                or ("google" if "gemini" in type(client).__name__.lower() else "anthropic")
            )
            self._model = model or settings.default_model
        self._max_tokens = max_tokens or settings.anthropic_max_tokens

    @property
    def supports_multimodal(self) -> bool:
        return llm_factory.supports_multimodal(self._provider)  # type: ignore[arg-type]

    async def analyze(
        self,
        clauses: list[Clause],
        *,
        filename: str | None = None,
        image_parts: Optional[list[tuple[bytes, str]]] = None,
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

        # Multimodal images are only forwarded when the provider supports them
        # (Gemini today). Anthropic also supports image inputs but the demo's
        # tool schema and prompt are tuned for the text-clause path; we'd
        # need a separate prompt to flip Anthropic into multimodal mode too.
        kwargs: dict[str, Any] = dict(
            system=ANALYZER_SYSTEM_PROMPT,
            user=user_prompt,
            model=self._model,
            max_tokens=self._max_tokens,
            tool_name=ANALYZER_TOOL_NAME,
            tool_description=ANALYZER_TOOL_DESCRIPTION,
            input_schema=ANALYZER_TOOL_SCHEMA,
        )
        if image_parts and self.supports_multimodal:
            kwargs["image_parts"] = image_parts
        elif image_parts and not self.supports_multimodal:
            log.info(
                "analyzer: dropping %d image part(s) — provider %r does not "
                "support multimodal in this codebase.",
                len(image_parts),
                self._provider,
            )

        parsed = await self._client.create_with_tool(**kwargs)

        clause_index = {c.section_id: c for c in clauses}
        raw_findings = [
            item for item in parsed.get("findings", []) if isinstance(item, dict)
        ]
        raw_missing = [
            item for item in parsed.get("missing_clauses", []) if isinstance(item, dict)
        ]

        # Catch "Missing X" findings the analyst routed into `findings` — clause-grounding can't ground an absence claim.
        rerouted_to_missing = 0
        kept_findings: list[dict[str, Any]] = []
        for item in raw_findings:
            if _looks_like_missing_clause(item):
                raw_missing.append(_demote_to_missing(item))
                rerouted_to_missing += 1
            else:
                kept_findings.append(item)

        dropped_unknown_section = 0
        dropped_invented_quote = 0
        findings: list[Finding] = []
        for item in kept_findings:
            converted, drop_reason = _to_finding_with_reason(item, clause_index)
            if converted is not None:
                findings.append(converted)
            elif drop_reason == "unknown_section":
                dropped_unknown_section += 1
            if drop_reason == "invented_quote":
                # Counted alongside successful conversion — the finding kept
                # the rest of the data, just lost a fabricated quote.
                dropped_invented_quote += 1

        missing = [_to_missing_finding(item) for item in raw_missing]
        missing = [f for f in missing if f is not None]

        if dropped_unknown_section:
            log.warning(
                "analyzer: dropped %d finding(s) referencing unknown section_id",
                dropped_unknown_section,
            )

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
            raw_response=parsed,
            metadata={
                "raw_findings_count": len(raw_findings),
                "dropped_unknown_section": dropped_unknown_section,
                "dropped_invented_quote": dropped_invented_quote,
                "rerouted_to_missing": rerouted_to_missing,
            },
        )


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


def _to_finding_with_reason(
    raw: dict[str, Any],
    clause_index: dict[str, Clause],
) -> tuple[Optional[Finding], Optional[str]]:
    section_id = str(raw.get("section_id") or "").strip()
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    if not section_id or not title or not summary:
        return None, "incomplete"

    # Drop the finding if it points at a clause the analyzer hallucinated.
    if section_id != "missing" and section_id not in clause_index:
        log.warning(
            "analyzer: dropping finding pointing at unknown section_id=%s",
            section_id,
        )
        return None, "unknown_section"

    quote = raw.get("clause_quote")
    invented_quote = False
    if isinstance(quote, str):
        quote = quote.strip() or None
        # If the quote isn't literally in the referenced clause, strip it —
        # the verifier will still score the claim against the clause text.
        clause = clause_index.get(section_id)
        if quote and clause and quote not in clause.text:
            invented_quote = True
            quote = None
    else:
        quote = None

    finding = Finding(
        section_id=section_id,
        title=title,
        risk=_coerce_risk(raw.get("risk"), default=RiskLevel.WARNING),
        category=_coerce_category(raw.get("category")),
        summary=summary,
        recommendation=str(raw.get("recommendation") or "").strip(),
        clause_quote=quote,
    )
    return finding, ("invented_quote" if invented_quote else None)


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


# Trailing space matters: matches "Missing X" but not "Missing-quote finding".
_LIKELY_MISSING_TITLE_PREFIXES = (
    "missing ",
    "lack of ",
    "absence of ",
    "absent ",
    "no ",
)
_LIKELY_MISSING_SUMMARY_PHRASES = (
    "the contract does not contain",
    "the contract contains no",
    "the contract lacks",
    "the contract is missing",
    "the document does not contain",
    "the document lacks",
    "there is no clause",
    "there is no provision",
    "no provision for",
    "does not include a ",
)


def _looks_like_missing_clause(raw: dict[str, Any]) -> bool:
    title = str(raw.get("title") or "").strip().lower()
    summary = str(raw.get("summary") or "").strip().lower()
    if any(title.startswith(p) for p in _LIKELY_MISSING_TITLE_PREFIXES):
        return True
    return any(phrase in summary for phrase in _LIKELY_MISSING_SUMMARY_PHRASES)


def _demote_to_missing(raw: dict[str, Any]) -> dict[str, Any]:
    risk = str(raw.get("risk") or "").lower().strip()
    if risk not in {"critical", "warning"}:
        risk = "warning"
    return {
        "title": raw.get("title") or "",
        "risk": risk,
        "summary": raw.get("summary") or "",
        "recommendation": raw.get("recommendation") or "",
    }
