from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from engine.app.services.anthropic_client import TokenUsage
from engine.config import settings

log = logging.getLogger(__name__)


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_DEFAULT_TIMEOUT = 60.0


# Module-level httpx client. Lazily created on first use and reused across all
# proxied calls so we keep TCP/TLS connections warm. FastAPI shuts down the
# client via `aclose_proxy_client()` on app shutdown.
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    timeout=_DEFAULT_TIMEOUT,
                    limits=httpx.Limits(
                        max_connections=20, max_keepalive_connections=10
                    ),
                )
    return _client


async def aclose_proxy_client() -> None:
    """FastAPI shutdown hook. Safe to call when no client was created."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class ProxyAuthError(RuntimeError):
    """Raised when the caller can't satisfy the proxy's auth requirements
    (e.g., Vertex AI credentials, which the OpenAI-compat path doesn't accept).
    Distinct from upstream/transport errors so callers can fail fast with a
    clear message instead of retrying a 401."""


async def proxy_chat_completion(
    *,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    response_format: Optional[dict[str, Any]] = None,
    lobstertrap_metadata: Optional[dict[str, Any]] = None,
) -> tuple[str, TokenUsage, dict[str, Any]]:
    """Single round-trip through the Lobster Trap OpenAI-compat endpoint.

    Returns `(content, token_usage, security_event)`. Raises on non-2xx
    after retries, on `length` finish reason (truncated output), and on
    missing/empty content.

    Auth model: Lobster Trap forwards the supplied bearer token to the
    upstream provider. Callers without a usable bearer token (e.g., Gemini
    Vertex AI service-account auth) MUST raise before calling this and route
    around the proxy or re-mint a token themselves.
    """
    if not api_key:
        raise ProxyAuthError(
            "Lobster Trap proxy requires a bearer token. Vertex AI service-"
            "account auth is not supported on the proxy path; either set "
            "GEMINI_API_KEY (AI Studio mode) or disable LOBSTERTRAP_ENABLED."
        )

    url = f"{settings.lobstertrap_base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if lobstertrap_metadata:
        payload["_lobstertrap"] = lobstertrap_metadata

    client = await _get_client()
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(
                    f"Lobster Trap proxy transport failed after "
                    f"{_MAX_RETRIES} attempts: {exc}"
                ) from exc
            await asyncio.sleep(2 ** attempt)
            continue

        if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES - 1:
            wait = 2 ** attempt
            log.warning(
                "Lobster Trap proxy returned %d (attempt %d), retrying in %ds",
                resp.status_code, attempt + 1, wait,
            )
            await asyncio.sleep(wait)
            continue

        if resp.status_code != 200:
            # Non-retryable, or we've exhausted retries.
            raise RuntimeError(
                f"Lobster Trap proxy failed with status {resp.status_code}: "
                f"{resp.text}"
            )

        data = resp.json()
        # Build the per-call security event. Body wins over headers when both
        # are present. We compute `intent_mismatch` ourselves so a proxy that
        # doesn't populate it (or lies about it) can't suppress the signal.
        declared_intent = (lobstertrap_metadata or {}).get("intent")
        event: dict[str, Any] = {
            "intent_declared": declared_intent,
            "risk_score": resp.headers.get("x-lobstertrap-risk-score"),
            "intent_detected": resp.headers.get("x-lobstertrap-intent-detected"),
            "action": resp.headers.get("x-lobstertrap-action"),
        }
        body_meta = data.get("_lobstertrap") if isinstance(data, dict) else None
        if isinstance(body_meta, dict):
            event.update(body_meta)
        detected = event.get("intent_detected")
        event["intent_mismatch"] = bool(
            declared_intent and detected and declared_intent != detected
        )

        # Surface a DENY action to the caller — the proxy already blocked
        # the request, but the response body may be a placeholder that would
        # otherwise be parsed as the model's actual answer.
        if (event.get("action") or "").upper() == "DENY":
            raise RuntimeError(
                f"Lobster Trap denied the request "
                f"(intent_detected={detected!r}, "
                f"risk_score={event.get('risk_score')!r})."
            )

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Lobster Trap returned no choices.")
        choice = choices[0]
        finish = (choice.get("finish_reason") or "").lower()
        if finish == "length":
            raise RuntimeError(
                f"Lobster Trap response was truncated at max_tokens={max_tokens}. "
                "Increase the per-provider max_tokens or shorten the input."
            )

        content = (choice.get("message") or {}).get("content") or ""
        usage_dict = data.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(usage_dict.get("prompt_tokens") or 0),
            output_tokens=int(usage_dict.get("completion_tokens") or 0),
        )
        return content, usage, event

    # Defensive: loop should always either return or raise above.
    raise RuntimeError(
        f"Lobster Trap proxy failed without a definitive response: {last_exc}"
    )
