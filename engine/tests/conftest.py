from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_engine_state():
    """Clear response cache and rate limiter between tests so monkeypatched
    pipelines aren't shadowed by a cache hit from a prior test."""
    from engine.app import main as main_module

    main_module._report_cache.clear()
    main_module._rate_limiter.reset()
    yield
    main_module._report_cache.clear()
    main_module._rate_limiter.reset()
