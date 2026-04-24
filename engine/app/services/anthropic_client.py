from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from anthropic import AsyncAnthropic


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


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


class ClaudeClient(Protocol):
    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
    ) -> str: ...


@dataclass
class AnthropicClient:
    """Thin wrapper so tests can substitute a fake without touching the SDK.

    After each call, `last_usage` holds the input/output token counts reported
    by the API. Pipeline components read this to build a per-run ledger.
    """

    api_key: str
    _client: Optional[AsyncAnthropic] = None
    last_usage: TokenUsage = field(default_factory=TokenUsage)

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
    ) -> str:
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
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)
