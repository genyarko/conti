from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from anthropic import AsyncAnthropic


class LLMClient(Protocol):
    """Provider-agnostic interface every demo-app adapter implements."""

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
    ) -> str: ...

    async def create_with_tool(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        tool_name: str,
        tool_description: str,
        input_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


# Back-compat alias: callers import `ClaudeClient`.
ClaudeClient = LLMClient


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

    async def create_with_tool(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        tool_name: str,
        tool_description: str,
        input_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Force the model to call a single tool and return its parsed input.

        Tool use guarantees the response matches the JSON schema, so we don't
        have to chase unescaped quotes, trailing commas, or smart quotes that
        plain text-mode prompts routinely emit on legal/contract content.
        """
        assert self._client is not None
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": tool_name,
                    "description": tool_description,
                    "input_schema": input_schema,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"Anthropic tool call was truncated at max_tokens={max_tokens}. "
                "Increase ANTHROPIC_MAX_TOKENS or shorten the input."
            )
        for block in resp.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == tool_name
            ):
                payload = getattr(block, "input", None)
                if isinstance(payload, dict):
                    return payload
                raise RuntimeError(
                    f"Tool {tool_name!r} returned non-dict input: {type(payload).__name__}"
                )
        raise RuntimeError(
            f"Model did not invoke the {tool_name!r} tool "
            f"(stop_reason={getattr(resp, 'stop_reason', None)!r})."
        )
