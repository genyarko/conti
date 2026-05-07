from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _reset_engine_state():
    """Clear response cache and rate limiter between tests so monkeypatched
    pipelines aren't shadowed by a cache hit from a prior test."""
    from engine.app import main as main_module

    await main_module._report_cache.clear()
    await main_module._rate_limiter.reset()
    yield
    await main_module._report_cache.clear()
    await main_module._rate_limiter.reset()
