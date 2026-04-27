from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from engine.app.services.anthropic_client import AnthropicClient, LLMClient
from engine.app.services.gemini_client import GeminiClient
from engine.config import settings

log = logging.getLogger(__name__)

Provider = Literal["anthropic", "google"]


@dataclass(frozen=True)
class Resolved:
    """The concrete (provider, model, fast_model) a pipeline run will use."""

    provider: Provider
    model: str
    fast_model: str


def resolve(
    provider: Optional[str] = None, model: Optional[str] = None
) -> Resolved:
    """Coerce caller-supplied (provider, model) to a concrete pair.

    - When both are omitted → use the configured server-side safe default.
    - When only `provider` is supplied → use that provider's flagship model.
    - When only `model` is supplied → infer provider from the model name.
    - Unknown provider strings fall back to the safe default with a warning.
    """
    if provider is None and model is None:
        return _default()

    if provider is None and model:
        provider = _infer_provider_from_model(model)

    p = (provider or "").lower().strip()
    if p == "anthropic":
        return Resolved(
            provider="anthropic",
            model=model or settings.anthropic_model,
            fast_model=settings.anthropic_fast_model,
        )
    if p in ("google", "gemini"):
        return Resolved(
            provider="google",
            model=model or settings.gemini_model,
            fast_model=settings.gemini_fast_model,
        )

    log.warning(
        "Unknown provider %r — falling back to default %r/%r.",
        provider,
        settings.default_provider,
        settings.default_model,
    )
    return _default()


def get_client(provider: Provider) -> LLMClient:
    """Construct the adapter for `provider`. Raises if its key is missing."""
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; cannot use the Anthropic provider."
            )
        return AnthropicClient(api_key=settings.anthropic_api_key)
    if provider == "google":
        if settings.gemini_use_vertex:
            if not settings.gemini_project:
                raise RuntimeError(
                    "GEMINI_USE_VERTEX=true but GEMINI_PROJECT is not set."
                )
            return GeminiClient(
                use_vertex=True,
                project=settings.gemini_project,
                location=settings.gemini_location or "global",
                credentials_json=settings.google_credentials_json,
            )
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set; cannot use the Google provider. "
                "Set GEMINI_USE_VERTEX=true (with GEMINI_PROJECT) for Vertex AI instead."
            )
        return GeminiClient(api_key=settings.gemini_api_key)
    raise RuntimeError(f"Unknown provider {provider!r}")


def is_available(provider: Provider) -> bool:
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "google":
        if settings.gemini_use_vertex:
            return bool(settings.gemini_project)
        return bool(settings.gemini_api_key)
    return False


def max_tokens_for(model: str) -> int:
    """Pick the right per-call max-tokens budget for a given model name."""
    name = (model or "").lower().strip()
    if name.startswith("gemini"):
        return settings.gemini_max_tokens
    return settings.anthropic_max_tokens


def _default() -> Resolved:
    p: Provider = settings.default_provider  # type: ignore[assignment]
    if p == "anthropic":
        return Resolved(
            provider="anthropic",
            model=settings.default_model or settings.anthropic_fast_model,
            fast_model=settings.anthropic_fast_model,
        )
    return Resolved(
        provider="google",
        model=settings.default_model or settings.gemini_fast_model,
        fast_model=settings.gemini_fast_model,
    )


def _infer_provider_from_model(model: str) -> str:
    name = model.lower().strip()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gemini"):
        return "google"
    return ""
