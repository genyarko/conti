from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from anthropic import AsyncAnthropic


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_input_tokens

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens


@dataclass
class TokenLedger:
    """Shared counter a pipeline run accumulates into across stages."""

    usage: TokenUsage = field(default_factory=TokenUsage)

    def record_from(self, client: object) -> None:
        last = getattr(client, "last_usage", None)
        if isinstance(last, TokenUsage):
            self.usage.add(last)

    def reset(self) -> None:
        self.usage = TokenUsage()


class LLMClient(Protocol):
    """Provider-agnostic interface every adapter (Anthropic, Gemini, …) implements.

    `response_schema` is the JSON schema callers expect the response to match.
    Providers that natively enforce structured output (Gemini) MUST pass it
    through; providers that don't (Anthropic) MAY ignore it — the prose schema
    inside their system prompts already steers the model.

    `lobstertrap_metadata` is an optional block passed to Lobster Trap security
    proxy. It contains declared intent and request context.
    """

    last_usage: TokenUsage
    # One entry per Lobster Trap-routed call. Adapters append; the orchestrator
    # aggregates across all calls in a run. List (not dict) so high-risk hits
    # earlier in a stage aren't overwritten by benign later calls.
    security_events: list[dict[str, Any]]

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        response_schema: Optional[dict[str, Any]] = None,
        lobstertrap_metadata: Optional[dict[str, Any]] = None,
    ) -> str: ...


# Back-compat alias: existing call sites import `ClaudeClient`.
ClaudeClient = LLMClient


@dataclass
class AnthropicClient:
    """Thin wrapper so tests can substitute a fake without touching the SDK.

    After each call, `last_usage` holds the input/output token counts reported
    by the API. Pipeline components read this to build a per-run ledger.
    """

    api_key: str
    _client: Optional[AsyncAnthropic] = None
    last_usage: TokenUsage = field(default_factory=TokenUsage)
    security_events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for the Anthropic client.")
        self._client = AsyncAnthropic(api_key=self.api_key)

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        response_schema: Optional[dict[str, Any]] = None,
        lobstertrap_metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        # Anthropic's messages API doesn't take a structured-output schema —
        # the prompt itself already says "respond with JSON only", and tests
        # rely on that. We accept the kwarg for protocol parity with Gemini
        # so the same call site works across providers.
        del response_schema

        from engine.config import settings
        if settings.lobstertrap_enabled and settings.lobstertrap_base_url:
            return await self._create_message_lobstertrap(
                system=system,
                user=user,
                model=model,
                max_tokens=max_tokens,
                lobstertrap_metadata=lobstertrap_metadata,
            )

        assert self._client is not None
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage = getattr(resp, "usage", None)
        self.last_usage = TokenUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
        # Reject truncated responses up-front: a max_tokens stop produces a
        # mid-sentence string that downstream JSON parsers can't recover.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"Anthropic response was truncated at max_tokens={max_tokens}. "
                "Increase ANTHROPIC_MAX_TOKENS or shorten the input."
            )
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)

    async def _create_message_lobstertrap(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        lobstertrap_metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        # Lazy import keeps the module importable without httpx in environments
        # that don't enable the proxy.
        from engine.app.services.lobstertrap import proxy_chat_completion

        content, usage, event = await proxy_chat_completion(
            api_key=self.api_key,
            model=model,
            system=system,
            user=user,
            max_tokens=max_tokens,
            lobstertrap_metadata=lobstertrap_metadata,
        )
        self.last_usage = usage
        self.security_events.append(event)
        return content
