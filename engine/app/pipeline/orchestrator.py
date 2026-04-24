from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

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
from engine.app.services.anthropic_client import TokenLedger
from engine.app.services.pricing import estimate_cost_usd
from engine.config import settings

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

    def _components(self, ledger: Optional[TokenLedger] = None) -> tuple[
        ClaimExtractor, ClaimGrounder, ConsistencyChecker, ReportAggregator
    ]:
        extractor = self.extractor
        if extractor is None:
            extractor = ClaimExtractor(ledger=ledger)
        elif ledger is not None and hasattr(extractor, "_ledger") and extractor._ledger is None:
            extractor._ledger = ledger

        grounder = self.grounder
        if grounder is None:
            grounder = ClaimGrounder(ledger=ledger)
        elif ledger is not None and hasattr(grounder, "_ledger") and grounder._ledger is None:
            grounder._ledger = ledger

        consistency = self.consistency
        if consistency is None:
            consistency = ConsistencyChecker(ledger=ledger)
        elif ledger is not None and hasattr(consistency, "_ledger") and consistency._ledger is None:
            consistency._ledger = ledger

        return (
            extractor,
            grounder,
            consistency,
            self.aggregator or ReportAggregator(),
        )

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
            ),
            6,
        )

    async def run(self, request: VerifyRequest) -> IntegrityReport:
        ledger = TokenLedger()
        extractor, grounder, consistency, aggregator = self._components(ledger)
        metadata = ReportMetadata(model=settings.anthropic_model)
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
        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.last_evidence = _build_evidence(claims, groundings, consistencies)
        return report

    async def run_quick(self, request: VerifyRequest) -> IntegrityReport:
        """Grounding-only fast path: skip the consistency LLM stage.

        Each claim receives a neutral `CONSISTENT` verdict so the aggregator's
        scoring still works, but no consistency API calls are made. Useful when
        callers just want a cheap "is this supported by the source?" check.
        """
        ledger = TokenLedger()
        extractor, grounder, _consistency, aggregator = self._components(ledger)
        metadata = ReportMetadata(model=settings.anthropic_model)
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
        self, source_context: str, claims: list[Claim]
    ) -> IntegrityReport:
        """Skip extraction and verify caller-supplied claims directly."""
        ledger = TokenLedger()
        _extractor, grounder, consistency, aggregator = self._components(ledger)
        metadata = ReportMetadata(model=settings.anthropic_model)
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
        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.last_evidence = _build_evidence(claims, groundings, consistencies)
        return report


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
