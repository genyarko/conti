from fastapi.testclient import TestClient

from engine.app.main import app

client = TestClient(app)


def test_root():
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "TrustLayer Engine"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "default_provider" in body
    assert "default_model" in body
    assert "providers" in body
    assert {"anthropic", "google"}.issubset(body["providers"].keys())
