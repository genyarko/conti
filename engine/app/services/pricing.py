from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# (input_price_per_mtok_usd, output_price_per_mtok_usd).
# Keep entries conservative — these feed an on-stage cost estimate.
_MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # Claude 4.x family.
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4": (1.0, 5.0),
}

_warned_models: set[str] = set()


def _lookup_price(model: str) -> tuple[float, float] | None:
    key = (model or "").strip().lower()
    if not key:
        return None
    if key in _MODEL_PRICES_USD_PER_MTOK:
        return _MODEL_PRICES_USD_PER_MTOK[key]
    # Fuzzy match on family prefix so e.g. "claude-opus-4-6-20260101" still hits.
    for prefix, price in _MODEL_PRICES_USD_PER_MTOK.items():
        if key.startswith(prefix):
            return price
    return None


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = _lookup_price(model)
    if price is None:
        if model and model not in _warned_models:
            log.warning("No price entry for model %r; cost estimate will be 0.", model)
            _warned_models.add(model)
        return 0.0
    in_rate, out_rate = price
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
