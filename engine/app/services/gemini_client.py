from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from engine.app.services.anthropic_client import TokenUsage

log = logging.getLogger(__name__)


def _import_genai():
    """Lazy import so this module loads even when google-genai isn't installed.

    Anthropic-only deployments don't need the Gemini SDK; deferring the import
    means `pip install google-genai` is only required for callers that
    actually instantiate a `GeminiClient`.
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:
        raise RuntimeError(
            "google-genai is not installed. Run `pip install google-genai` "
            "to enable the Gemini provider."
        ) from exc
    return genai, genai_types


def _credentials_from_json(raw: str) -> Any:
    """Parse a service-account JSON blob and return google-auth Credentials.

    Production deploys (Render, Vercel, Railway, etc.) don't have the gcloud
    CLI, so ADC isn't available. The caller passes the contents of a service-
    account key file as an env-var-friendly JSON string instead.
    """
    from google.oauth2 import service_account

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON is not valid JSON."
        ) from exc
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )


@dataclass
class GeminiClient:
    """Thin wrapper around google-genai so tests can substitute a fake.

    Mirrors the shape of `AnthropicClient.create_message` so the pipeline can
    accept either client behind the `LLMClient` protocol. After each call,
    `last_usage` holds the token counts reported by Gemini's `usage_metadata`.

    Two auth modes:
    - AI Studio (default): pass `api_key=...`. Bills via the AI Studio
      prepay pool tied to the project the key belongs to.
    - Vertex AI / "Agent Platform" (`use_vertex=True` + `project`): no
      api_key — auth via Application Default Credentials, billed against
      the Cloud project's billing account (so $300 trial credit applies).

    Multimodal: pass `image_parts` as a list of (bytes, mime_type) tuples.
    Each part becomes an inline_data Part prepended to the user turn — the
    contract analyzer uses this to send rendered PDF pages directly.
    """

    api_key: str = ""
    use_vertex: bool = False
    project: str = ""
    location: str = "global"
    # Optional service-account JSON (contents of a key file). When set
    # together with use_vertex, auth uses these credentials instead of
    # Application Default Credentials. Required on prod deploys (Render,
    # Vercel, Railway, …) where the gcloud CLI isn't available.
    credentials_json: str = ""
    _client: Any = None
    last_usage: TokenUsage = field(default_factory=TokenUsage)
    security_events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        genai, _ = _import_genai()
        if self.use_vertex:
            if not self.project:
                raise RuntimeError(
                    "GEMINI_PROJECT is required when GEMINI_USE_VERTEX=true."
                )
            kwargs: dict[str, Any] = {
                "vertexai": True,
                "project": self.project,
                "location": self.location or "global",
            }
            if self.credentials_json:
                kwargs["credentials"] = _credentials_from_json(
                    self.credentials_json
                )
            self._client = genai.Client(**kwargs)
        else:
            if not self.api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is required for the Gemini AI Studio client. "
                    "Set GEMINI_USE_VERTEX=true to use Vertex AI / Cloud billing instead."
                )
            self._client = genai.Client(api_key=self.api_key)

    async def create_message(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        response_schema: Optional[dict[str, Any]] = None,
        image_parts: Optional[list[tuple[bytes, str]]] = None,
        lobstertrap_metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """Generate a text response. Returns the raw model output as a string.

        When `response_schema` is provided, the model is instructed to emit
        JSON that matches the schema (Gemini structured output). When
        `image_parts` is provided, each image is sent as an inline_data Part
        before the user text — used by the multimodal contract analyzer.
        """
        from engine.config import settings
        if settings.lobstertrap_enabled and settings.lobstertrap_base_url:
            if image_parts:
                log.warning("Gemini multimodal input is not supported through Lobster Trap proxy.")
            return await self._create_message_lobstertrap(
                system=system,
                user=user,
                model=model,
                max_tokens=max_tokens,
                response_schema=response_schema,
                lobstertrap_metadata=lobstertrap_metadata,
            )

        assert self._client is not None
        _, genai_types = _import_genai()
        contents = self._build_contents(user, image_parts, genai_types)
        
        # Verification pipelines often process adversarial or "unsafe" samples 
        # to test grounding; disable safety filters to prevent silent failures.
        safety_settings = [
            genai_types.SafetySetting(
                category=cat,
                threshold="BLOCK_NONE",
            )
            for cat in [
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
                "HARM_CATEGORY_CIVIC_INTEGRITY",
            ]
        ]

        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            response_mime_type=(
                "application/json" if response_schema else "text/plain"
            ),
            response_schema=response_schema,
            safety_settings=safety_settings,
        )

        # Gemini Pro has tight quotas (2 RPM on free tier); add robust retries.
        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                break
            except Exception as exc:
                exc_str = str(exc).lower()
                # 429 (quota), 503 (overloaded), 500 (internal error) are retryable.
                # google-genai SDK also uses "resource_exhausted" and "deadline_exceeded".
                is_retryable = any(
                    code in exc_str 
                    for code in ["429", "503", "500", "resource_exhausted", "deadline_exceeded", "quota", "overloaded"]
                )
                if is_retryable and attempt < max_retries - 1:
                    # Longer backoff for 429s as 2 RPM means we might need ~30s.
                    wait = (attempt + 1) * 5 
                    log.warning("Gemini %s failed (attempt %d): %s. Retrying in %ds...", model, attempt + 1, exc_str, wait)
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"Gemini API call failed after {attempt+1} attempts: {exc}") from exc

        self._record_usage(resp)
        self._reject_truncated(resp, max_tokens)
        return self._extract_text(resp)

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
        image_parts: Optional[list[tuple[bytes, str]]] = None,
        lobstertrap_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Force the model to call a single function and return its parsed args.

        Gemini's function-calling API guarantees the response matches the
        declared schema, so callers don't have to chase trailing commas or
        smart quotes from plain-text JSON output. Equivalent to Anthropic's
        `tool_choice={"type":"tool","name":...}`.
        """
        from engine.config import settings
        if settings.lobstertrap_enabled and settings.lobstertrap_base_url:
            # Fall back to structured output via create_message since tool-call
            # proxying is more complex in OpenAI format.
            log.info("Gemini tool call falling back to structured output through Lobster Trap.")
            resp = await self.create_message(
                system=system,
                user=user,
                model=model,
                max_tokens=max_tokens,
                response_schema=input_schema,
                lobstertrap_metadata=lobstertrap_metadata,
            )
            return json.loads(resp)

        assert self._client is not None
        _, genai_types = _import_genai()
        contents = self._build_contents(user, image_parts, genai_types)
        function_decl = genai_types.FunctionDeclaration(
            name=tool_name,
            description=tool_description,
            parameters=input_schema,
        )
        tool = genai_types.Tool(function_declarations=[function_decl])
        
        safety_settings = [
            genai_types.SafetySetting(
                category=cat,
                threshold="BLOCK_NONE",
            )
            for cat in [
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
                "HARM_CATEGORY_CIVIC_INTEGRITY",
            ]
        ]

        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            tools=[tool],
            tool_config=genai_types.ToolConfig(
                function_calling_config=genai_types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=[tool_name],
                ),
            ),
            safety_settings=safety_settings,
        )

        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                break
            except Exception as exc:
                exc_str = str(exc).lower()
                is_retryable = any(
                    code in exc_str 
                    for code in ["429", "503", "500", "resource_exhausted", "deadline_exceeded", "quota", "overloaded"]
                )
                if is_retryable and attempt < max_retries - 1:
                    wait = (attempt + 1) * 5
                    log.warning("Gemini tool %s failed (attempt %d): %s. Retrying in %ds...", model, attempt + 1, exc_str, wait)
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"Gemini tool call failed after {attempt+1} attempts: {exc}") from exc

        self._record_usage(resp)
        self._reject_truncated(resp, max_tokens)
        return self._extract_function_args(resp, tool_name)

    async def _create_message_lobstertrap(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        response_schema: Optional[dict[str, Any]] = None,
        lobstertrap_metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        # Lazy import: avoids a circular at module load and keeps the proxy
        # path optional for deployments that don't enable Lobster Trap.
        from engine.app.services.lobstertrap import (
            ProxyAuthError,
            proxy_chat_completion,
        )

        if self.use_vertex:
            # Vertex auth is gRPC + service-account, not bearer-token. Bail
            # early with a clear message instead of sending `Authorization:
            # Bearer ` (empty) and getting an opaque 401.
            raise ProxyAuthError(
                "Lobster Trap proxy is not supported with GEMINI_USE_VERTEX=true. "
                "Either set GEMINI_API_KEY (AI Studio mode) or disable "
                "LOBSTERTRAP_ENABLED."
            )

        response_format: Optional[dict[str, Any]] = None
        if response_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": response_schema,
                },
            }

        content, usage, event = await proxy_chat_completion(
            api_key=self.api_key,
            model=model,
            system=system,
            user=user,
            max_tokens=max_tokens,
            response_format=response_format,
            lobstertrap_metadata=lobstertrap_metadata,
        )
        self.last_usage = usage
        self.security_events.append(event)
        return content

    @staticmethod
    def _build_contents(
        user: str,
        image_parts: Optional[list[tuple[bytes, str]]],
        genai_types: Any,
    ) -> list[Any]:
        parts: list[Any] = []
        if image_parts:
            for blob, mime in image_parts:
                parts.append(
                    genai_types.Part.from_bytes(data=blob, mime_type=mime)
                )
        if user:
            parts.append(genai_types.Part.from_text(text=user))
        return [genai_types.Content(role="user", parts=parts)]

    def _record_usage(self, resp: Any) -> None:
        usage = getattr(resp, "usage_metadata", None)
        self.last_usage = TokenUsage(
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        )

    @staticmethod
    def _reject_truncated(resp: Any, max_tokens: int) -> None:
        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            # If there are no candidates, it might be blocked by safety.
            # Check prompt feedback if available.
            feedback = getattr(resp, "prompt_feedback", None)
            block_reason = getattr(feedback, "block_reason", None)
            if block_reason:
                raise RuntimeError(f"Gemini blocked the prompt: {block_reason}")
            # Even if no block_reason is found, an empty response is an error for our pipeline.
            raise RuntimeError("Gemini returned an empty response (no candidates).")

        cand = candidates[0]
        finish = getattr(cand, "finish_reason", None)
        finish_str = (
            getattr(finish, "name", None) or str(finish or "")
        ).upper()

        if finish_str == "MAX_TOKENS":
            raise RuntimeError(
                f"Gemini response was truncated at max_tokens={max_tokens}. "
                "Increase GEMINI_MAX_TOKENS or shorten the input."
            )
        
        if finish_str in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "OTHER"):
            raise RuntimeError(f"Gemini blocked the response: {finish_str}")

    @staticmethod
    def _extract_text(resp: Any) -> str:
        """Safely extract text from the response, avoiding property-access errors."""
        # Try direct .text access first (SDK preferred way)
        try:
            return resp.text
        except Exception:
            pass

        # Fallback: walk candidates → content → parts.
        parts: list[str] = []
        for cand in getattr(resp, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                t = getattr(part, "text", None)
                if t:
                    parts.append(t)
        return "".join(parts)

    @staticmethod
    def _extract_function_args(resp: Any, tool_name: str) -> dict[str, Any]:
        for cand in getattr(resp, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                fn = getattr(part, "function_call", None)
                if fn is None:
                    continue
                if getattr(fn, "name", None) != tool_name:
                    continue
                args = getattr(fn, "args", None)
                if isinstance(args, dict):
                    return args
                # Some SDK versions return a Struct-like proto; coerce via dict().
                try:
                    return dict(args)  # type: ignore[arg-type]
                except Exception:
                    raise RuntimeError(
                        f"Gemini tool {tool_name!r} returned non-dict args: "
                        f"{type(args).__name__}"
                    )
        finish = None
        cands = getattr(resp, "candidates", None) or []
        if cands:
            finish = getattr(cands[0], "finish_reason", None)
        raise RuntimeError(
            f"Gemini did not invoke the {tool_name!r} function "
            f"(finish_reason={finish!r})."
        )
