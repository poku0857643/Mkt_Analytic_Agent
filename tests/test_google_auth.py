"""Sign in with Google: redirect, callback checks, session cookie, CSRF, access rules.

Google's token endpoint and ID-token verification are replaced with stubs; the
rest (state, nonce, PKCE, cookies, roles, audit) runs for real.
"""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app.google_auth as google_auth
from app.api import app, get_audit_log
from app.auth import SESSION_COOKIE, SESSION_PURPOSE
from app.config import Settings, get_settings
from app.sessions import sign, verify

SECRET = "test-session-secret-0123456789abcdef"
CLIENT_ID = "test-client.apps.googleusercontent.com"


class MemoryAuditLog:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


def make_settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        gcp_project="test-project",
        role_datasets={"analyst": ["marketing"], "admin": ["marketing", "customers"]},
        google_client_id=CLIENT_ID,
        google_client_secret="test-client-secret",
        session_secret=SECRET,
        user_roles={"ana@example.com": "analyst", "@company.com": "admin"},
        session_cookie_secure=False,  # TestClient talks plain http
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def audit():
    return MemoryAuditLog()


@pytest.fixture
def client(settings, audit):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_audit_log] = lambda: audit
    yield TestClient(app)
    app.dependency_overrides.clear()


class FakeGoogle:
    """Stands in for Google's token endpoint and ID-token verification."""

    def __init__(self, monkeypatch):
        self.claims = {}
        self.seen = {}

        async def exchange_code(code, verifier, redirect_uri, settings):
            self.seen = {"code": code, "verifier": verifier, "redirect_uri": redirect_uri}
            return "raw-id-token"

        def verify_id_token(token, settings):
            assert token == "raw-id-token"
            return dict(self.claims)

        monkeypatch.setattr(google_auth, "exchange_code", exchange_code)
        monkeypatch.setattr(google_auth, "verify_id_token", verify_id_token)


@pytest.fixture
def google(monkeypatch):
    return FakeGoogle(monkeypatch)


def start_login(client):
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    return parse_qs(urlparse(response.headers["location"]).query)


def sign_in(client, google, email="ana@example.com", **claims):
    params = start_login(client)
    google.claims = {"email": email, "email_verified": True, "nonce": params["nonce"][0], "name": "Ana", **claims}
    return client.get(
        "/auth/callback",
        params={"code": "one-time-code", "state": params["state"][0]},
        follow_redirects=False,
    )


# ---------- sessions ----------


def test_signed_token_round_trip_and_tampering():
    token = sign({"email": "a@x.com"}, "session", SECRET, 60)
    assert verify(token, "session", SECRET)["email"] == "a@x.com"
    assert verify(token, "oauth", SECRET) is None  # wrong purpose
    assert verify(token, "session", "other-secret") is None
    body, mac = token.split(".")
    forged = base64.urlsafe_b64encode(b'{"email":"boss@x.com","exp":9999999999,"typ":"session"}').rstrip(b"=").decode()
    assert verify(f"{forged}.{mac}", "session", SECRET) is None
    assert verify("garbage", "session", SECRET) is None
    assert verify(None, "session", SECRET) is None


def test_signed_token_expires():
    token = sign({"email": "a@x.com"}, "session", SECRET, 60, now=1000)
    assert verify(token, "session", SECRET, now=1059) is not None
    assert verify(token, "session", SECRET, now=1061) is None


# ---------- configuration ----------


def test_disabled_without_configuration(audit):
    app.dependency_overrides[get_settings] = lambda: make_settings(google_client_secret=None)
    try:
        client = TestClient(app)
        assert client.get("/auth/session").json()["google_enabled"] is False
        assert client.get("/auth/login", follow_redirects=False).status_code == 404
    finally:
        app.dependency_overrides.clear()


# ---------- login redirect ----------


def test_login_redirects_to_google_with_pkce(client):
    response = client.get("/auth/login", follow_redirects=False)
    location = urlparse(response.headers["location"])
    params = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == google_auth.AUTHORIZE_URL
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == ["http://testserver/auth/callback"]
    assert params["scope"] == ["openid email profile"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"][0] and params["nonce"][0]

    cookie = response.headers["set-cookie"]
    assert google_auth.STATE_COOKIE in cookie
    assert "HttpOnly" in cookie and "Path=/auth" in cookie and "SameSite=lax" in cookie


def test_configured_redirect_uri_is_used(audit):
    app.dependency_overrides[get_settings] = lambda: make_settings(oauth_redirect_uri="https://answers.example.com/auth/callback")
    try:
        params = start_login(TestClient(app))
        assert params["redirect_uri"] == ["https://answers.example.com/auth/callback"]
    finally:
        app.dependency_overrides.clear()


def test_cookies_are_secure_by_default(audit):
    app.dependency_overrides[get_settings] = lambda: make_settings(session_cookie_secure=True)
    try:
        response = TestClient(app).get("/auth/login", follow_redirects=False)
        assert "Secure" in response.headers["set-cookie"]
    finally:
        app.dependency_overrides.clear()


# ---------- callback ----------


def test_successful_sign_in(client, google, audit):
    params = start_login(client)
    google.claims = {"email": "Ana@Example.com", "email_verified": True, "nonce": params["nonce"][0], "name": "Ana"}
    response = client.get(
        "/auth/callback", params={"code": "one-time-code", "state": params["state"][0]}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE in response.headers.get("set-cookie", "")

    # PKCE: the verifier sent to Google matches the challenge in the redirect.
    challenge = base64.urlsafe_b64encode(hashlib.sha256(google.seen["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == [challenge]
    assert google.seen["code"] == "one-time-code"

    assert client.get("/whoami").json() == {
        "user": "ana@example.com",
        "role": "analyst",
        "allowed_datasets": ["marketing"],
    }
    assert client.get("/auth/session").json()["email"] == "ana@example.com"
    [record] = audit.records
    assert (record.event, record.outcome, record.user, record.role) == ("sign_in", "signed_in", "ana@example.com", "analyst")


@pytest.mark.parametrize(
    "tamper, reason",
    [
        ("state", "expired"),
        ("no_cookie", "expired"),
        ("nonce", "failed"),
        ("unverified", "unverified"),
        ("google_error", "cancelled"),
    ],
)
def test_callback_rejections(client, google, audit, tamper, reason):
    params = start_login(client)
    google.claims = {"email": "ana@example.com", "email_verified": True, "nonce": params["nonce"][0]}
    query = {"code": "one-time-code", "state": params["state"][0]}
    if tamper == "state":
        query["state"] = "attacker-state"
    elif tamper == "no_cookie":
        client.cookies.clear()
    elif tamper == "nonce":
        google.claims["nonce"] = "replayed-nonce"
    elif tamper == "unverified":
        google.claims["email_verified"] = False
    elif tamper == "google_error":
        query = {"error": "access_denied"}

    response = client.get("/auth/callback", params=query, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/?signin_error={reason}"
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert client.get("/whoami").status_code == 401
    assert audit.records[-1].outcome == "denied"


def test_unknown_person_is_signed_in_but_waits_for_access(client, google, audit):
    response = sign_in(client, google, email="newcomer@gmail.com")
    assert response.headers["location"] == "/"
    whoami = client.get("/whoami")
    assert whoami.status_code == 403
    assert "does not have access yet" in whoami.json()["detail"]
    assert audit.records[-1].outcome == "access_requested"
    assert audit.records[-1].user == "newcomer@gmail.com"


def test_domain_rule_needs_google_hosted_domain(client, google):
    sign_in(client, google, email="cara@company.com", hd="company.com")
    assert client.get("/whoami").json()["role"] == "admin"

    client.cookies.clear()
    # Same address shape, but not a Workspace account of company.com.
    sign_in(client, google, email="cara@company.com")
    assert client.get("/whoami").status_code == 403


def test_access_is_rechecked_on_every_request(client, google, settings):
    sign_in(client, google)
    assert client.get("/whoami").status_code == 200
    settings.user_roles.pop("ana@example.com")
    assert client.get("/whoami").status_code == 403


# ---------- session use ----------


def test_cookie_writes_need_the_csrf_header(client, google):
    sign_in(client, google)
    # A short question fails validation (422) only after authentication passes.
    assert client.post("/ask", json={"question": "x"}).status_code == 403
    assert client.post("/ask", json={"question": "x"}, headers={"X-Requested-With": "fetch"}).status_code == 422


def test_forged_or_expired_session_is_rejected(client):
    client.cookies.set(SESSION_COOKIE, sign({"email": "ana@example.com"}, SESSION_PURPOSE, "wrong-secret", 3600))
    assert client.get("/whoami").status_code == 401
    client.cookies.set(SESSION_COOKIE, sign({"email": "ana@example.com"}, SESSION_PURPOSE, SECRET, -10))
    assert client.get("/whoami").status_code == 401
    # A state token can't be replayed as a session.
    client.cookies.set(SESSION_COOKIE, sign({"email": "ana@example.com"}, google_auth.STATE_PURPOSE, SECRET, 3600))
    assert client.get("/whoami").status_code == 401


def test_logout(client, google):
    sign_in(client, google)
    assert client.post("/auth/logout").status_code == 403  # no CSRF header
    response = client.post("/auth/logout", headers={"X-Requested-With": "fetch"})
    assert response.status_code == 200
    assert client.get("/whoami").status_code == 401


def test_api_keys_still_work_alongside_sessions(audit):
    from app.auth import hash_key
    from app.config import KeyOwner

    app.dependency_overrides[get_settings] = lambda: make_settings(
        api_keys={hash_key("script-key"): KeyOwner(user="etl-bot", role="analyst")}
    )
    try:
        response = TestClient(app).get("/whoami", headers={"X-API-Key": "script-key"})
        assert response.json()["user"] == "etl-bot"
    finally:
        app.dependency_overrides.clear()
