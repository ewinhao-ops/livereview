"""A fresh installation must work without its original private SSO platform."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app, _login_fails


@pytest.fixture
def client(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "password")
    monkeypatch.setattr(s, "access_password", "unit-test-password-123")
    monkeypatch.setattr(s, "secret_key", "unit-test-cookie-signing-secret")
    monkeypatch.setattr(s, "public_base_url", "http://testserver")
    monkeypatch.setattr(s, "cookie_secure", False)
    _login_fails.clear()
    with TestClient(app, client=("203.0.113.10", 12345)) as c:
        yield c


def test_password_login_logout_and_data_gate(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/sessions").status_code == 401
    assert client.post("/api/login", json={"password": "incorrect"}).status_code == 401
    response = client.post("/api/login", json={"password": "unit-test-password-123"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/me").json()["auth"] == "password"
    assert client.get("/api/sessions").status_code == 200
    assert client.post("/api/logout").json()["logout_url"] == "/"
    assert client.get("/api/me").status_code == 401


def test_cookie_tamper_and_password_rotation(client, monkeypatch):
    client.post("/api/login", json={"password": "unit-test-password-123"})
    monkeypatch.setattr(get_settings(), "access_password", "new-unit-test-password")
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()
    client.cookies.set("fupan_emergency", "9999999999.invalid-signature")
    assert client.get("/api/me").status_code == 401


def test_cross_origin_login_and_rate_limit(client):
    assert client.post("/api/login", headers={"Origin": "https://foreign.example"},
                       json={"password": "unit-test-password-123"}).status_code == 403
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 429


def test_unconfigured_or_short_password_is_not_usable(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "access_password", "short")
    assert client.post("/api/login", json={"password": "short"}).status_code == 503
