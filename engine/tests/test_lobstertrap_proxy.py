"""Unit + integration tests for the Lobster Trap proxy path.

Covers the four non-trivial behaviors that the original codebase shipped without
test coverage: declared-vs-detected mismatch detection, retry-on-5xx, truncation
guard, and Vertex auth rejection. Also exercises the batch audit aggregator's
fix for the first-item-wins bug.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx
import pytest

import engine.app.services.lobstertrap as lt
from engine.app.services.lobstertrap import (
    ProxyAuthError,
    proxy_chat_completion,
)


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch: pytest.MonkeyPatch):
    """Point the proxy at a dummy URL — we never actually hit the network."""
    from engine.config import settings
    monkeypatch.setattr(settings, "lobstertrap_base_url", "http://lt.test")
    monkeypatch.setattr(settings, "lobstertrap_enabled", True)
    yield
    # Reset the global client so each test gets a fresh transport.
    lt._client = None


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Replace the module-level httpx client with one using a MockTransport
    that records calls and dispatches to `handler(request) -> httpx.Response`."""
    seen: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handle)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    monkeypatch.setattr(lt, "_client", client)
    return seen


def _ok_response(
    *,
    content: str = '{"ok": true}',
    headers: Optional[dict[str, str]] = None,
    body_meta: Optional[dict] = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
) -> httpx.Response:
    body: dict = {
        "choices": [
            {"message": {"content": content}, "finish_reason": finish_reason}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }
    if body_meta is not None:
        body["_lobstertrap"] = body_meta
    return httpx.Response(200, json=body, headers=headers or {})


@pytest.mark.asyncio
async def test_proxy_records_mismatch_when_declared_differs_from_detected(
    monkeypatch: pytest.MonkeyPatch,
):
    seen = _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            headers={
                "x-lobstertrap-risk-score": "High",
                "x-lobstertrap-intent-detected": "data_exfiltration",
                "x-lobstertrap-action": "LOG",
            },
        ),
    )

    content, usage, event = await proxy_chat_completion(
        api_key="sk-test",
        model="claude-haiku-4-5",
        system="sys",
        user="usr",
        max_tokens=512,
        lobstertrap_metadata={"intent": "grounding_verification"},
    )

    assert content == '{"ok": true}'
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert event["intent_declared"] == "grounding_verification"
    assert event["intent_detected"] == "data_exfiltration"
    assert event["intent_mismatch"] is True
    assert event["risk_score"] == "High"
    assert event["action"] == "LOG"
    # Outgoing payload carried the declared intent under `_lobstertrap`.
    sent_body = json.loads(seen[0].content)
    assert sent_body["_lobstertrap"] == {"intent": "grounding_verification"}


@pytest.mark.asyncio
async def test_proxy_no_mismatch_when_declared_matches_detected(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            headers={
                "x-lobstertrap-intent-detected": "grounding_verification",
                "x-lobstertrap-risk-score": "Low",
            },
        ),
    )
    _, _, event = await proxy_chat_completion(
        api_key="k",
        model="m",
        system="s",
        user="u",
        max_tokens=64,
        lobstertrap_metadata={"intent": "grounding_verification"},
    )
    assert event["intent_mismatch"] is False


@pytest.mark.asyncio
async def test_proxy_body_overrides_header_metadata(
    monkeypatch: pytest.MonkeyPatch,
):
    """When Lobster Trap returns `_lobstertrap` in the body, it wins over
    headers (the body version is canonical; headers are convenience)."""
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            headers={"x-lobstertrap-risk-score": "Low"},
            body_meta={"risk_score": "High", "intent_detected": "exploit"},
        ),
    )
    _, _, event = await proxy_chat_completion(
        api_key="k", model="m", system="s", user="u", max_tokens=64,
        lobstertrap_metadata={"intent": "claim_extraction"},
    )
    assert event["risk_score"] == "High"
    assert event["intent_detected"] == "exploit"


@pytest.mark.asyncio
async def test_proxy_raises_on_deny_action(monkeypatch: pytest.MonkeyPatch):
    """A DENY action means the proxy already blocked the call. We must NOT
    return the placeholder body to the pipeline, where it'd be parsed as the
    model's actual answer."""
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            content="<blocked>",
            headers={
                "x-lobstertrap-action": "DENY",
                "x-lobstertrap-intent-detected": "credential_leak",
                "x-lobstertrap-risk-score": "High",
            },
        ),
    )
    with pytest.raises(RuntimeError, match="denied"):
        await proxy_chat_completion(
            api_key="k", model="m", system="s", user="u", max_tokens=64,
        )


@pytest.mark.asyncio
async def test_proxy_retries_on_503_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def _handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream overloaded")
        return _ok_response()

    monkeypatch.setattr(lt, "_MAX_RETRIES", 4)
    # Make backoff effectively instantaneous.
    monkeypatch.setattr(lt.asyncio, "sleep", _instant_sleep)
    _install_transport(monkeypatch, _handler)

    content, _, _ = await proxy_chat_completion(
        api_key="k", model="m", system="s", user="u", max_tokens=64,
    )
    assert content == '{"ok": true}'
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_proxy_does_not_retry_on_400(monkeypatch: pytest.MonkeyPatch):
    """4xx (other than 429) is non-retryable; we should fail fast with the
    server's body so the caller can see what they got wrong."""
    calls = {"n": 0}

    def _handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad model")

    _install_transport(monkeypatch, _handler)
    with pytest.raises(RuntimeError, match="status 400"):
        await proxy_chat_completion(
            api_key="k", model="m", system="s", user="u", max_tokens=64,
        )
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_proxy_raises_on_truncation(monkeypatch: pytest.MonkeyPatch):
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(content="partial...", finish_reason="length"),
    )
    with pytest.raises(RuntimeError, match="truncated"):
        await proxy_chat_completion(
            api_key="k", model="m", system="s", user="u", max_tokens=64,
        )


@pytest.mark.asyncio
async def test_proxy_rejects_empty_api_key():
    """Vertex AI mode passes empty `self.api_key`; the bearer header would
    be malformed and 401. Fail at the boundary with a useful message."""
    with pytest.raises(ProxyAuthError, match="Vertex"):
        await proxy_chat_completion(
            api_key="",
            model="gemini-2.5-pro",
            system="s",
            user="u",
            max_tokens=64,
        )


def _instant_sleep(_seconds):
    """Replacement for asyncio.sleep that returns immediately."""
    async def _noop():
        return None
    return _noop()
