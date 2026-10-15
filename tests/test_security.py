"""Authentication, per-user isolation, CSRF/origin protection, lockout, encryption and fail-closed start-up (SRS 20.4)."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from pyapp import auth, vault
from pyapp.config import settings
from pyapp.main import app

ADMIN_PASSWORD = "Correct-Horse-123"


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "SECRET_KEY", "s" * 40)
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 4)
    auth.limiter.failures.clear()
    asyncio.run(auth.bootstrap_admin())
    return TestClient(app)


def login(client, username="admin", password=ADMIN_PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_everything_requires_a_session_except_health_login_and_static(secured):
    client = secured
    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code in (200, 404)               # login page is public
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/runs").status_code == 401
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.get("/evidence/anything.webp", follow_redirects=False).status_code in (303, 401)
    assert login(client).status_code == 200
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/auth/me").json()["user"]["username"] == "admin"
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/dashboard").status_code == 401


def test_wrong_passwords_are_rejected_and_locked_out_without_revealing_which_part_failed(secured):
    client = secured
    messages = {login(client, "admin", "nope-nope-nope").json()["detail"], login(client, "ghost", "nope-nope-nope").json()["detail"]}
    assert messages == {"Invalid username or password."}
    for _ in range(4):
        login(client, "admin", "nope-nope-nope")
    locked = login(client, "admin", ADMIN_PASSWORD)
    assert locked.status_code == 429                                    # even the right password is refused while locked


def test_users_cannot_see_each_others_strategies_or_conversations(secured):
    admin = secured
    assert login(admin).status_code == 200
    assert admin.post("/api/users", json={"username": "bob", "password": "Another-Long-Pass-9"}).status_code == 201
    admin.post("/api/library/custom", json={"name": "Admin private idea"})
    bob = TestClient(app)
    assert login(bob, "bob", "Another-Long-Pass-9").status_code == 200
    assert bob.get("/api/library/strategies").json() == []
    bob.post("/api/library/custom", json={"name": "Bob private idea"})
    assert [s["name"] for s in bob.get("/api/library/strategies").json()] == ["Bob private idea"]
    assert [s["name"] for s in admin.get("/api/library/strategies").json()] == ["Admin private idea"]
    assert bob.get("/api/users").status_code == 403                     # not an administrator
    assert bob.get("/api/admin/backup").status_code == 403
    bob_id = bob.get("/api/library/strategies").json()[0]["id"]
    assert admin.get(f"/api/library/strategies/{bob_id}").status_code == 404


def test_cross_site_writes_are_blocked_and_security_headers_present(secured):
    client = secured
    login(client)
    blocked = client.post("/api/library/custom", json={"name": "x" * 5}, headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403
    allowed = client.post("/api/library/custom", json={"name": "legit"}, headers={"Origin": "http://testserver"})
    assert allowed.status_code == 201
    headers = client.get("/api/health").headers
    assert headers["x-content-type-options"] == "nosniff" and "frame-ancestors" in headers["content-security-policy"] and headers["x-frame-options"] == "SAMEORIGIN"


def test_session_cookie_is_httponly_and_tamper_proof(secured):
    client = secured
    response = login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    client.cookies.set(auth.COOKIE, "forged.token.value")
    assert client.get("/api/dashboard").status_code == 401


def test_changing_the_password_invalidates_other_sessions(secured):
    a, b = secured, TestClient(app)
    login(a)
    login(b)
    assert a.post("/api/auth/password", json={"current": ADMIN_PASSWORD, "new": "Brand-New-Password-7"}).status_code == 200
    assert b.get("/api/dashboard").status_code == 401                   # the other session no longer matches the password version
    assert a.get("/api/dashboard").status_code == 200
    assert a.post("/api/auth/password", json={"current": "wrong", "new": "Whatever-Long-1"}).status_code == 403


def test_passwords_are_hashed_with_scrypt_and_weak_ones_refused():
    stored = auth.hash_password("Correct-Horse-123")
    assert stored.startswith("scrypt$") and "Correct" not in stored
    assert auth.verify_password("Correct-Horse-123", stored) and not auth.verify_password("correct-horse-123", stored)
    with pytest.raises(ValueError):
        auth.check_password_strength("short")


def test_server_refuses_to_start_unprotected_in_production(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "ALLOW_OPEN_ACCESS", False)
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        asyncio.run(auth.bootstrap_admin())
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        asyncio.run(auth.bootstrap_admin())


def test_stored_credentials_are_encrypted(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "k" * 40)
    token = vault.encrypt("1//refresh-token-value")
    assert "refresh-token-value" not in token and vault.decrypt(token) == "1//refresh-token-value"
    monkeypatch.setattr(settings, "SECRET_KEY", "z" * 40)
    with pytest.raises(ValueError):
        vault.decrypt(token)                                            # a different key can never read it


def test_spreadsheet_formula_injection_is_neutralised():
    from pyapp.exporters import safe, trades_csv
    assert safe("=HYPERLINK(\"http://evil\")") == "'=HYPERLINK(\"http://evil\")"
    assert safe("@cmd") == "'@cmd" and safe("normal text") == "normal text" and safe("-5") == "-5"
    csv_text = trades_csv([{"strategy": "=1+1", "tradeId": "T1"}]).decode("utf-8-sig")
    assert "'=1+1" in csv_text
