from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from engine.app.models.schemas import (
    Claim,
    ConsistencyVerdict,
    IntegrityReport,
    ReportMetadata,
    VerifyRequest,
)
from engine.app.pipeline.aggregator import ReportAggregator
from engine.app.pipeline.consistency import ConsistencyChecker, ConsistencyResult
from engine.app.pipeline.extractor import ClaimExtractor
from engine.app.pipeline.grounder import ClaimGrounder
from engine.config import settings

log = logging.getLogger(__name__)


@dataclass
class VerifyPipeline:
    extractor: Optional[ClaimExtractor] = None
    grounder: Optional[ClaimGrounder] = None
    consistency: Optional[ConsistencyChecker] = None
    aggregator: Optional[ReportAggregator] = None

    def _components(self) -> tuple[
        ClaimExtractor, ClaimGrounder, ConsistencyChecker, ReportAggregator
    ]:
        return (
            self.extractor or ClaimExtractor(),
            self.grounder or ClaimGrounder(),
            self.consistency or ConsistencyChecker(),
            self.aggregator or ReportAggregator(),
        )

    async def run(self, request: VerifyRequest) -> IntegrityReport:
        extractor, grounder, consistency, aggregator = self._components()
        metadata = ReportMetadata(model=settings.anthropic_model)

        t0 = time.perf_counter()
        extraction = await extractor.extract(request.llm_output)
        claims = extraction.claims
        t1 = time.perf_counter()
        metadata.extractor_ms = int((t1 - t0) * 1000)

        if not claims:
            metadata.duration_ms = metadata.extractor_ms
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

        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        return report

    async def run_quick(self, request: VerifyRequest) -> IntegrityReport:
        """Grounding-only fast path: skip the consistency LLM stage.

        Each claim receives a neutral `CONSISTENT` verdict so the aggregator's
        scoring still works, but no consistency API calls are made. Useful when
        callers just want a cheap "is this supported by the source?" check.
        """
        extractor, grounder, _consistency, aggregator = self._components()
        metadata = ReportMetadata(model=settings.anthropic_model)

        t0 = time.perf_counter()
        extraction = await extractor.extract(request.llm_output)
        claims = extraction.claims
        metadata.extractor_ms = int((time.perf_counter() - t0) * 1000)

        if not claims:
            metadata.duration_ms = metadata.extractor_ms
            return aggregator.aggregate(
                claims=[], groundings=[], consistencies=[], metadata=metadata
            )

        g_start = time.perf_counter()
        groundings = await grounder.ground_many(claims, request.source_context)
        metadata.grounder_ms = int((time.perf_counter() - g_start) * 1000)
        metadata.consistency_ms = 0

        consistencies = [_skipped_consistency(c.id) for c in claims]
        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        return report

    async def run_with_claims(
        self, source_context: str, claims: list[Claim]
    ) -> IntegrityReport:
        """Skip extraction and verify caller-supplied claims directly."""
        _extractor, grounder, consistency, aggregator = self._components()
        metadata = ReportMetadata(model=settings.anthropic_model)
        metadata.extractor_ms = 0

        t0 = time.perf_counter()
        if not claims:
            metadata.duration_ms = 0
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

        report = aggregator.aggregate(
            claims=claims,
            groundings=groundings,
            consistencies=consistencies,
            metadata=metadata,
        )
        report.metadata.duration_ms = int((time.perf_counter() - t0) * 1000)
        return report


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
