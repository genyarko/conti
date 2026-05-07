from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from engine.app import main as main_module
from engine.app.main import app


client = TestClient(app)


@pytest.fixture
def _auth_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module.settings, "api_auth_token", "secret-token")
    yield
    monkeypatch.setattr(main_module.settings, "api_auth_token", "")


def test_new_get_endpoints_require_auth_when_token_configured(_auth_enabled):
    for path in ("/stats", "/audit/events", "/verify/trace/req_missing"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"


def test_public_endpoints_remain_open_with_auth_enabled(_auth_enabled):
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_stats_with_valid_token_succeeds(_auth_enabled):
    r = client.get("/stats", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    assert "metrics" in r.json()


@dataclass
class _DummyClient:
    host: str


@dataclass
class _DummyRequest:
    headers: dict
    client: _DummyClient


def test_client_key_ignores_xff_when_proxy_untrusted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module.settings, "trusted_proxy_ips", [])
    req = _DummyRequest(
        headers={"x-forwarded-for": "198.51.100.1"},
        client=_DummyClient(host="10.0.0.2"),
    )
    assert main_module._client_key(req) == "10.0.0.2"


def test_client_key_honors_xff_when_proxy_trusted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module.settings, "trusted_proxy_ips", ["10.0.0.2"])
    req = _DummyRequest(
        headers={"x-forwarded-for": "198.51.100.1, 10.0.0.2"},
        client=_DummyClient(host="10.0.0.2"),
    )
    assert main_module._client_key(req) == "198.51.100.1"


# ---------- scope mapping ----------


def test_required_scope_maps_routes_to_scopes():
    rs = main_module._required_scope
    assert rs("GET", "/stats") == "stats:read"
    assert rs("GET", "/audit/events") == "audit:read"
    assert rs("GET", "/verify/trace/req_abc") == "verify:read"
    assert rs("POST", "/verify") == "verify:write"
    assert rs("POST", "/verify/quick") == "verify:write"
    assert rs("POST", "/verify/claims") == "verify:write"
    assert rs("POST", "/verify/batch") == "verify:write"
    # No scope for public/unmatched routes — they go through unscoped.
    assert rs("GET", "/health") is None
    assert rs("GET", "/") is None


# ---------- scope enforcement ----------


class _StubApiKeyService:
    """Maps bearer tokens to ApiKey instances for scope tests."""

    def __init__(self, mapping: dict[str, "main_module.ApiKey"]):
        self._mapping = mapping

    async def resolve(self, bearer: str):
        return self._mapping.get(bearer.strip())


@pytest.fixture
def _scoped_keys(monkeypatch: pytest.MonkeyPatch):
    from engine.app.services.api_keys import ApiKey

    monkeypatch.setattr(main_module.settings, "api_auth_token", "secret-token")
    keys = {
        "read-only": ApiKey(
            id="key_ro",
            name="Read Only",
            daily_usd_cap=None,
            daily_token_cap=None,
            scopes=("stats:read", "audit:read", "verify:read"),
        ),
        "write-only": ApiKey(
            id="key_wo",
            name="Write Only",
            daily_usd_cap=None,
            daily_token_cap=None,
            scopes=("verify:write",),
        ),
        "wildcard": ApiKey(
            id="key_all",
            name="Wildcard",
            daily_usd_cap=None,
            daily_token_cap=None,
            scopes=("*",),
        ),
        "legacy": ApiKey(
            id="key_legacy",
            name="Legacy",
            daily_usd_cap=None,
            daily_token_cap=None,
            scopes=None,  # unrestricted (rows that predate scope enforcement)
        ),
    }
    monkeypatch.setattr(
        main_module, "_api_key_service", _StubApiKeyService(keys)
    )
    yield keys
    monkeypatch.setattr(main_module.settings, "api_auth_token", "")


def test_read_only_token_blocked_on_verify_post(_scoped_keys):
    r = client.post(
        "/verify",
        json={"source_context": "s", "llm_output": "o"},
        headers={"Authorization": "Bearer read-only"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "forbidden_scope"
    assert body["required_scope"] == "verify:write"


def test_read_only_token_allowed_on_stats(_scoped_keys):
    r = client.get("/stats", headers={"Authorization": "Bearer read-only"})
    assert r.status_code == 200


def test_read_only_token_allowed_on_audit(_scoped_keys):
    r = client.get(
        "/audit/events", headers={"Authorization": "Bearer read-only"}
    )
    assert r.status_code == 200


def test_write_only_token_blocked_on_stats(_scoped_keys):
    r = client.get("/stats", headers={"Authorization": "Bearer write-only"})
    assert r.status_code == 403
    assert r.json()["required_scope"] == "stats:read"


def test_wildcard_token_allowed_everywhere(_scoped_keys):
    r = client.get("/stats", headers={"Authorization": "Bearer wildcard"})
    assert r.status_code == 200
    r = client.get(
        "/audit/events", headers={"Authorization": "Bearer wildcard"}
    )
    assert r.status_code == 200


def test_legacy_null_scopes_unrestricted(_scoped_keys):
    """Keys created before scopes existed (NULL scopes column) keep full
    access, so the migration doesn't break existing partners."""
    r = client.get("/stats", headers={"Authorization": "Bearer legacy"})
    assert r.status_code == 200


def test_invalid_token_still_returns_401_not_403(_scoped_keys):
    r = client.get(
        "/stats", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert r.status_code == 401

