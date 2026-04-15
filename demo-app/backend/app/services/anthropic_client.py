from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from anthropic import AsyncAnthropic


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
    """Thin wrapper around the async Anthropic SDK so tests can swap in a fake."""

    api_key: str
    _client: Optional[AsyncAnthropic] = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required.")
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
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)
