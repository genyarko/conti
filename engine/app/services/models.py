from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from engine.app.services import llm_factory
from engine.app.services.pricing import _lookup_price

Tier = Literal["flagship", "balanced", "fast"]


@dataclass(frozen=True)
class ModelEntry:
    provider: str  # "anthropic" | "google"
    id: str
    label: str
    tier: Tier


# Single source of truth for the user-facing model picker. Pricing is read
# from `pricing.py` so that's the only place rates need to be updated.
_CATALOG: tuple[ModelEntry, ...] = (
    # Google Gemini — listed first so the dropdown shows the demo default first.
    # 3.x preview IDs only resolve via AI Studio; 2.5 IDs work on both AI
    # Studio and Vertex AI / "Agent Platform" (the path that bills via Cloud).
    ModelEntry(
        provider="google",
        id="gemini-3.1-pro-preview",
        label="Gemini 3.1 Pro (preview · AI Studio)",
        tier="flagship",
    ),
    ModelEntry(
        provider="google",
        id="gemini-3-flash-preview",
        label="Gemini 3 Flash (preview · AI Studio)",
        tier="fast",
    ),
    ModelEntry(
        provider="google",
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        tier="flagship",
    ),
    ModelEntry(
        provider="google",
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        tier="fast",
    ),
    # Anthropic Claude — kept for the provider-agnostic story.
    ModelEntry(
        provider="anthropic",
        id="claude-opus-4-7",
        label="Claude Opus 4.7",
        tier="flagship",
    ),
    ModelEntry(
        provider="anthropic",
        id="claude-sonnet-4-6",
        label="Claude Sonnet 4.6",
        tier="balanced",
    ),
    ModelEntry(
        provider="anthropic",
        id="claude-haiku-4-5",
        label="Claude Haiku 4.5",
        tier="fast",
    ),
)


def list_models() -> list[dict]:
    """Return the catalog as plain dicts for the `/models` endpoint.

    Each entry includes `available: bool` so the frontend can grey out
    providers whose API keys aren't configured rather than 500 on submit.
    """
    out: list[dict] = []
    for entry in _CATALOG:
        price = _lookup_price(entry.id) or (0.0, 0.0)
        out.append(
            {
                "provider": entry.provider,
                "id": entry.id,
                "label": entry.label,
                "tier": entry.tier,
                "input_price_per_mtok": price[0],
                "output_price_per_mtok": price[1],
                "available": llm_factory.is_available(entry.provider),  # type: ignore[arg-type]
            }
        )
    return out


def is_known(provider: str, model: str) -> bool:
    return any(
        e.provider == provider and e.id == model for e in _CATALOG
    )
