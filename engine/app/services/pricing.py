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
    # Gemini 3.x preview family. Confirm against Google pricing before billing.
    # Conservative placeholders modeled on the 2.x → 3.x preview-tier delta.
    "gemini-3.1-pro-preview-customtools": (2.5, 15.0),
    "gemini-3.1-pro-preview": (2.5, 15.0),
    "gemini-3.1-pro": (2.5, 15.0),
    "gemini-3-flash-preview": (0.30, 1.20),
    "gemini-3-flash": (0.30, 1.20),
    # Gemini 2.5 family (the IDs Vertex AI actually serves).
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
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


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Calculate the estimated USD cost of an LLM call.

    Anthropic prompt caching (cache_read_tokens) is charged at a 90% discount
    relative to standard input tokens.
    """
    price = _lookup_price(model)
    if price is None:
        if model and model not in _warned_models:
            log.warning("No price entry for model %r; cost estimate will be 0.", model)
            _warned_models.add(model)
        return 0.0
    in_rate, out_rate = price
    # cache_read_tokens are 10% of the normal input rate.
    cache_cost = (cache_read_tokens / 1_000_000) * in_rate * 0.1
    input_cost = (input_tokens / 1_000_000) * in_rate
    output_cost = (output_tokens / 1_000_000) * out_rate
    return input_cost + output_cost + cache_cost
