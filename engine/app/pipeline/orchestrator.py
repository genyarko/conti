from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from engine.app.models.schemas import (
    Claim,
    ConsistencyVerdict,
    IntegrityReport,
    ReportMetadata,
    TraceClaimEvidence,
    VerifyRequest,
)
from engine.app.pipeline.aggregator import ReportAggregator
from engine.app.pipeline.consistency import ConsistencyChecker, ConsistencyResult
from engine.app.pipeline.extractor import ClaimExtractor
from engine.app.pipeline.grounder import ClaimGrounder, GroundingResult
from engine.app.services import llm_factory
from engine.app.services.anthropic_client import TokenLedger
from engine.app.services.pricing import estimate_cost_usd

log = logging.getLogger(__name__)


@dataclass
class VerifyPipeline:
    extractor: Optional[ClaimExtractor] = None
    grounder: Optional[ClaimGrounder] = None
    consistency: Optional[ConsistencyChecker] = None
    aggregator: Optional[ReportAggregator] = None
    # Populated at the end of each run*() so callers can persist the
    # raw per-claim evidence alongside the aggregated report.
    last_evidence: list[TraceClaimEvidence] = field(default_factory=list)

    def _components(
        self,
        ledger: Optional[TokenLedger] = None,
        *,
        resolved: Optional[llm_factory.Resolved] = None,
        request_id: Optional[str] = None,
    ) -> tuple[
        ClaimExtractor, ClaimGrounder, ConsistencyChecker, ReportAggregator
    ]:
        # Caller-supplied components win unconditionally — that's how tests
        # inject stub clients without triggering real API calls. When a slot
        # is empty AND a resolver is given, build a fresh component bound to
        # the resolved provider's client, with the per-stage model split:
        # fast tier for extractor + grounder, flagship for consistency.
        client = (
            llm_factory.get_client(resolved.provider)
            if resolved is not None and (
                self.extractor is None
                or self.grounder is None
                or self.consistency is None
            )
            else None
        )

        extractor = self.extractor
        if extractor is None:
            if resolved is not None:
                extractor = ClaimExtractor(
                    client=client, 
                    model=resolved.fast_model, 
                    ledger=ledger,
                    request_id=request_id,
                )
            else:
                extractor = ClaimExtractor(ledger=ledger, request_id=request_id)
        elif (
            ledger is not None
            and hasattr(extractor, "_ledger")
            and extractor._ledger is None
        ):
            extractor._ledger = ledger
        if request_id and hasattr(extractor, "_request_id") and extractor._request_id is None:
            extractor._request_id = request_id

        grounder = self.grounder
        if grounder is None:
            if resolved is not None:
                grounder = ClaimGrounder(
                    client=client, 
                    model=resolved.fast_model, 
                    ledger=ledger,
                    request_id=request_id,
                )
            else:
                grounder = ClaimGrounder(ledger=ledger, request_id=request_id)
        elif (
            ledger is not None
            and hasattr(grounder, "_ledger")
            and grounder._ledger is None
        ):
            grounder._ledger = ledger
        if request_id and hasattr(grounder, "_request_id") and grounder._request_id is None:
            grounder._request_id = request_id

        consistency = self.consistency
        if consistency is None:
            if resolved is not None:
                consistency = ConsistencyChecker(
                    client=client,
                    model=resolved.model,
                    fast_model=resolved.fast_model,
                    ledger=ledger,
                    request_id=request_id,
                )
            else:
                consistency = ConsistencyChecker(ledger=ledger, request_id=request_id)
        elif (
            ledger is not None
            and hasattr(consistency, "_ledger")
            and consistency._ledger is None
        ):
            consistency._ledger = ledger
        if request_id and hasattr(consistency, "_request_id") and consistency._request_id is None:
            consistency._request_id = request_id

        return (
            extractor,
            grounder,
            consistency,
            self.aggregator or ReportAggregator(),
        )

    def _resolve(
        self, provider: Optional[str], model: Optional[str]
    ) -> llm_factory.Resolved:
        return llm_factory.resolve(provider=provider, model=model)

    def _new_metadata(self, resolved: llm_factory.Resolved, request_id: Optional[str] = None) -> ReportMetadata:
        # Stamp the metadata with the *resolved* provider+model that actually
        # ran — fixing the prior bug where metadata.model said "Opus" while
        # Haiku did the work. `fast_model` exposes the cheaper tier most calls
        # actually hit (extractor, grounder, source-consistency); `model` is
        # the flagship reserved for contradiction detection.
        kwargs: dict[str, Any] = {
            "provider": resolved.provider,
            "model": resolved.model,
            "fast_model": resolved.fast_model,
        }
        if request_id:
            kwargs["request_id"] = request_id
        return ReportMetadata(**kwargs)

    def _apply_usage(
        self, metadata: ReportMetadata, ledger: TokenLedger
    ) -> None:
        metadata.input_tokens = ledger.usage.input_tokens
        metadata.output_tokens = ledger.usage.output_tokens
        metadata.total_tokens = ledger.usage.total_tokens
        metadata.estimated_cost_usd = round(
            estimate_cost_usd(
                metadata.model,
                ledger.usage.input_tokens,
                ledger.usage.output_tokens,
                cache_read_tokens=ledger.usage.cache_read_input_tokens,
            ),
            6,
        )

    def _apply_security(
        self, metadata: ReportMetadata, clients: list[Optional[llm_factory.LLMClient]]
    ) -> None:
        """Aggregate Lobster Trap signals across every LLM call in the run.

        Each client carries a list of per-call events (one per `create_message`
        routed through the proxy). We escalate `risk_score` to the highest tier
        seen, OR-reduce `intent_mismatch`, and surface a representative event's
        labels — preferring a mismatched event since that's the more
        informative thing for an auditor to see.
        """
        risk_rank = {"Low": 1, "Medium": 2, "High": 3}
        # Action severity ranks the policy outcomes Lobster Trap can emit.
        # The audit row should show the most-impactful action, not whichever
        # one happened to land on the most-informative event.
        action_rank = {
            "ALLOW": 0, "LOG": 1, "RATE_LIMIT": 2,
            "HUMAN_REVIEW": 3, "QUARANTINE": 4, "DENY": 5,
        }
        events: list[dict[str, Any]] = []
        for client in clients:
            if not client:
                continue
            events.extend(getattr(client, "security_events", None) or [])

        if not events:
            return

        for ev in events:
            rs = ev.get("risk_score")
            if rs and risk_rank.get(rs, 0) > risk_rank.get(metadata.security_risk_score or "", 0):
                metadata.security_risk_score = rs
            action = (ev.get("action") or "").upper() or None
            if action and action_rank.get(action, 0) > action_rank.get(
                (metadata.security_action or "").upper(), 0
            ):
                metadata.security_action = action

        metadata.security_intent_mismatch = any(ev.get("intent_mismatch") for ev in events)

        # For the surfaced declared/detected labels, prefer a mismatched event
        # — that's the row an auditor cares about. Fall back to the first
        # event that carries any labels.
        representative = next(
            (ev for ev in events if ev.get("intent_mismatch")),
            next(
                (ev for ev in events if ev.get("intent_detected") or ev.get("intent_declared")),
                None,
            ),
        )
        if representative is not None:
            metadata.security_intent_detected = (
                representative.get("intent_detected") or metadata.security_intent_detected
            )
            metadata.security_intent_declared = (
                representative.get("intent_declared") or metadata.security_intent_declared
            )

    async def run(self, request: VerifyRequest, request_id: Optional[str] = None) -> IntegrityReport:
        ledger = TokenLedger()
        resolved = self._resolve(request.provider, request.model)
        metadata = self._new_metadata(resolved, request_id=request_id)
        request_id = metadata.request_id

        extractor, grounder, consistency, aggregator = self._components(
            ledger, resolved=resolved, request_id=request_id
        )
        self.last_evidence = []

        t0 = time.perf_counter()
        extraction = await extractor.extract(request.llm_output)
        claims = extraction.claims
        t1 = time.perf_counter()
        metadata.extractor_ms = int((t1 - t0) * 1000)

        if not claims:
            metadata.duration_ms = metadata.extractor_ms
            self._apply_usage(metadata, ledger)
            return aggregator.aggregate(
                claims=[],
                groundings=[],
                consistencies=[],
                metadata=metadata,
            )

        grounding_task = asyncio.create_task(
            grounder.ground_many(claims, request.source_context)
        )
        consistency_task = asyncio.create_task(
            consistency.check(claims, request.source_context)
        )
        g_start = time.perf_counter()
        groundings, consistencies = await asyncio.gather(
            grounding_task, consistency_task
        )
        g_end = time.perf_counter()
        # With parallel execution we can't attribute time per stage cleanly;
        # record the wall-clock span for the parallel block under both.
        span_ms = int((g_end - g_start) * 1000)
        metadata.grounder_ms = span_ms
        metadata.consistency_ms = span_ms

        self._apply_usage(metadata, ledger)
        self._apply_security(metadata, _stage_clients(extractor, grounder, consistency))
        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.last_evidence = _build_evidence(claims, groundings, consistencies)
        return report

    async def run_quick(self, request: VerifyRequest, request_id: Optional[str] = None) -> IntegrityReport:
        """Grounding-only fast path: skip the consistency LLM stage.

        Each claim receives a neutral `CONSISTENT` verdict so the aggregator's
        scoring still works, but no consistency API calls are made. Useful when
        callers just want a cheap "is this supported by the source?" check.
        """
        ledger = TokenLedger()
        resolved = self._resolve(request.provider, request.model)
        metadata = self._new_metadata(resolved, request_id=request_id)
        request_id = metadata.request_id
        
        extractor, grounder, _consistency, aggregator = self._components(
            ledger, resolved=resolved, request_id=request_id
        )
        # Quick mode runs only on the fast tier — record that, not the flagship.
        metadata.model = resolved.fast_model
        self.last_evidence = []

        t0 = time.perf_counter()
        extraction = await extractor.extract(request.llm_output)
        claims = extraction.claims
        metadata.extractor_ms = int((time.perf_counter() - t0) * 1000)

        if not claims:
            metadata.duration_ms = metadata.extractor_ms
            self._apply_usage(metadata, ledger)
            return aggregator.aggregate(
                claims=[], groundings=[], consistencies=[], metadata=metadata
            )

        g_start = time.perf_counter()
        groundings = await grounder.ground_many(claims, request.source_context)
        metadata.grounder_ms = int((time.perf_counter() - g_start) * 1000)
        metadata.consistency_ms = 0

        consistencies = [_skipped_consistency(c.id) for c in claims]
        self._apply_usage(metadata, ledger)
        self._apply_security(metadata, _stage_clients(extractor, grounder))
        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.last_evidence = _build_evidence(claims, groundings, consistencies)
        return report

    async def run_with_claims(
        self,
        source_context: str,
        claims: list[Claim],
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> IntegrityReport:
        """Skip extraction and verify caller-supplied claims directly."""
        ledger = TokenLedger()
        resolved = self._resolve(provider, model)
        metadata = self._new_metadata(resolved, request_id=request_id)
        request_id = metadata.request_id

        _extractor, grounder, consistency, aggregator = self._components(
            ledger, resolved=resolved, request_id=request_id
        )
        metadata.extractor_ms = 0
        self.last_evidence = []

        t0 = time.perf_counter()
        if not claims:
            metadata.duration_ms = 0
            self._apply_usage(metadata, ledger)
            return aggregator.aggregate(
                claims=[], groundings=[], consistencies=[], metadata=metadata
            )

        grounding_task = asyncio.create_task(
            grounder.ground_many(claims, source_context)
        )
        consistency_task = asyncio.create_task(
            consistency.check(claims, source_context)
        )
        g_start = time.perf_counter()
        groundings, consistencies = await asyncio.gather(
            grounding_task, consistency_task
        )
        span_ms = int((time.perf_counter() - g_start) * 1000)
        metadata.grounder_ms = span_ms
        metadata.consistency_ms = span_ms

        self._apply_usage(metadata, ledger)
        self._apply_security(metadata, _stage_clients(grounder, consistency))
        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.last_evidence = _build_evidence(claims, groundings, consistencies)
        return report


def _stage_clients(*stages: Any) -> list[Optional[llm_factory.LLMClient]]:
    """Return each stage's LLM client, or None for stages (e.g. test stubs)
    that don't expose one. Keeps `_apply_security` defensive at the boundary
    so we don't AttributeError on stages that don't follow the convention."""
    return [getattr(stage, "_client", None) for stage in stages]


def _build_evidence(
    claims: list[Claim],
    groundings: list[GroundingResult],
    consistencies: list[ConsistencyResult],
) -> list[TraceClaimEvidence]:
    """Fuse per-claim grounding + consistency results into the trace evidence
    the aggregator flattens away — matched passage/location, raw grounder and
    consistency reasoning, internal-contradiction links, and the fast-vs-
    semantic grounding path actually taken."""
    g_by_id = {g.claim_id: g for g in groundings}
    c_by_id = {c.claim_id: c for c in consistencies}
    out: list[TraceClaimEvidence] = []
    for claim in claims:
        grounding = g_by_id.get(claim.id)
        consistency = c_by_id.get(claim.id)
        if grounding is None or consistency is None:
            continue
        out.append(
            TraceClaimEvidence(
                claim_id=claim.id,
                text=claim.text,
                category=claim.category,
                output_quote=claim.output_quote,
                grounding_score=grounding.grounding_score,
                grounding_level=grounding.grounding_level,
                matched_passage=grounding.matched_passage,
                match_location=grounding.match_location,
                grounding_reasoning=grounding.reasoning,
                used_semantic_fallback=grounding.used_semantic_fallback,
                consistency_verdict=consistency.verdict,
                source_consistent=consistency.source_consistent,
                internal_consistent=consistency.internal_consistent,
                confidence=consistency.confidence,
                consistency_reasoning=consistency.reasoning,
                contradicts=list(consistency.contradicts),
            )
        )
    return out


def _skipped_consistency(claim_id: str) -> ConsistencyResult:
    return ConsistencyResult(
        claim_id=claim_id,
        verdict=ConsistencyVerdict.CONSISTENT,
        source_consistent=True,
        internal_consistent=True,
        confidence=5,
        reasoning="Consistency check skipped (quick mode).",
    )


async def verify(request: VerifyRequest) -> IntegrityReport:
    return await VerifyPipeline().run(request)
