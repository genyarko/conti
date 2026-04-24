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

