from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


def _import_genai():
    """Lazy import: only callers that actually instantiate GeminiClient need
    the SDK installed. Anthropic-only deployments stay unaffected."""
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
    """Parse a service-account JSON blob into google-auth Credentials.

    Production deploys don't have the gcloud CLI / ADC, so the caller passes
    the contents of a service-account key file as an env-var JSON string.
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
    """Demo-app Gemini adapter mirroring `AnthropicClient`'s interface.

    Exposes both `create_message` (plain text out) and `create_with_tool`
    (function-calling JSON) so the analyzer pipeline can swap providers
    without changing call sites. Multimodal PDF pages flow through the
    optional `image_parts` arg on either method (Phase G3).

    Two auth modes:
    - AI Studio (default): pass `api_key=...`. Bills via AI Studio prepay
      pool tied to the project the key belongs to.
    - Vertex AI / "Agent Platform" (`use_vertex=True` + `project`): no
      api_key — auth via Application Default Credentials, billed against
      the Cloud project's billing account.
    """

    api_key: str = ""
    use_vertex: bool = False
    project: str = ""
    location: str = "global"
    # Optional service-account JSON for prod deploys without gcloud ADC.
    credentials_json: str = ""
    _client: Any = None

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
        image_parts: Optional[list[tuple[bytes, str]]] = None,
    ) -> str:
        assert self._client is not None
        _, genai_types = _import_genai()
        contents = self._build_contents(user, image_parts, genai_types)
        
        # Disable safety filters to prevent silent failures on adversarial samples.
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
                    log.warning("Gemini %s failed (attempt %d): %s. Retrying in %ds...", model, attempt + 1, exc_str, wait)
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"Gemini API call failed after {attempt+1} attempts: {exc}") from exc

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
    ) -> dict[str, Any]:
        """Force the model to call a single function and return its parsed args.

        Equivalent to Anthropic `tool_choice={"type":"tool","name":...}`. The
        schema is enforced by Gemini, so the analyzer doesn't have to
        recover from trailing-comma JSON on legal text.
        """
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

        self._reject_truncated(resp, max_tokens)
        return self._extract_function_args(resp, tool_name)

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

    @staticmethod
    def _reject_truncated(resp: Any, max_tokens: int) -> None:
        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            # Check prompt feedback for block reason.
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
