"""Sign in with Google (OpenID Connect authorization-code flow with PKCE).

  GET  /auth/login     -> redirect to Google's sign-in page
  GET  /auth/callback  -> Google redirects back; verify, set the session cookie
  GET  /auth/session   -> who is signed in (for the web app), no auth needed
  POST /auth/logout    -> clear the session cookie

The page itself loads nothing from Google: the browser is simply redirected there
and back, so the web app's Content-Security-Policy stays same-origin only.
State, nonce and PKCE protect the round trip; the ID token is verified with
Google's public keys (signature, issuer, audience, expiry) before it is trusted.
"""
import asyncio
import base64
import hashlib
import secrets
import uuid
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.audit import AuditLog, AuditRecord, get_audit_log
from app.auth import (
    CSRF_HEADER,
    CSRF_VALUE,
    SESSION_COOKIE,
    SESSION_PURPOSE,
    read_session,
    role_for_email,
)
from app.config import Settings, get_settings
from app.sessions import sign, verify

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
STATE_COOKIE = "mkt_oauth"
STATE_PURPOSE = "oauth"
STATE_MAX_AGE = 600

router = APIRouter(prefix="/auth", include_in_schema=False)

_google_request = google_requests.Request()


def _require_google(settings: Settings) -> None:
    if not settings.google_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google sign-in is not configured")


def _redirect_uri(request: Request, settings: Settings) -> str:
    return settings.oauth_redirect_uri or str(request.url_for("auth_callback"))


def _set_cookie(response, name: str, value: str, max_age: int, settings: Settings, path: str = "/") -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=path,
        httponly=True,
        secure=settings.session_cookie_secure,
        # Lax: sent on the top-level redirect back from Google, never on
        # cross-site POSTs.
        samesite="lax",
    )


def _back_to_app(error: str | None = None) -> RedirectResponse:
    url = "/" if error is None else "/?" + urlencode({"signin_error": error})
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


async def exchange_code(code: str, verifier: str, redirect_uri: str, settings: Settings) -> str:
    """Swap the one-time code for tokens; returns the raw ID token."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret.get_secret_value(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
    response.raise_for_status()
    return response.json()["id_token"]


def verify_id_token(token: str, settings: Settings) -> dict:
    """Check signature, issuer, audience and expiry against Google's public keys."""
    return id_token.verify_oauth2_token(token, _google_request, settings.google_client_id)


@router.get("/session")
async def session_info(request: Request, settings: Annotated[Settings, Depends(get_settings)]):
    session = read_session(request, settings)
    return {
        "google_enabled": settings.google_enabled,
        "signed_in": session is not None,
        "email": session["email"] if session else None,
        "name": session.get("name") if session else None,
    }


@router.get("/login")
async def login(request: Request, settings: Annotated[Settings, Depends(get_settings)]):
    _require_google(settings)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(request, settings),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    response = RedirectResponse(f"{AUTHORIZE_URL}?{urlencode(params)}", status_code=status.HTTP_302_FOUND)
    secret = settings.session_secret.get_secret_value()
    _set_cookie(
        response,
        STATE_COOKIE,
        sign({"state": state, "nonce": nonce, "verifier": verifier}, STATE_PURPOSE, secret, STATE_MAX_AGE),
        STATE_MAX_AGE,
        settings,
        path="/auth",
    )
    return response


@router.get("/callback", name="auth_callback")
async def callback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditLog, Depends(get_audit_log)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    _require_google(settings)
    secret = settings.session_secret.get_secret_value()
    record = AuditRecord(request_id=str(uuid.uuid4()), event="sign_in", outcome="denied", http_status=303)

    def fail(reason: str, detail: str) -> RedirectResponse:
        record.error = detail
        audit.write(record)
        response = _back_to_app(reason)
        response.delete_cookie(STATE_COOKIE, path="/auth")
        return response

    if error:
        # The person cancelled on Google's page, or Google refused.
        return fail("cancelled", f"Google returned error={error[:100]}")

    saved = verify(request.cookies.get(STATE_COOKIE), STATE_PURPOSE, secret)
    if saved is None or not state or not secrets.compare_digest(saved["state"], state) or not code:
        return fail("expired", "Missing or mismatched OAuth state")

    try:
        raw = await exchange_code(code, saved["verifier"], _redirect_uri(request, settings), settings)
        claims = await asyncio.to_thread(verify_id_token, raw, settings)
    except (httpx.HTTPError, GoogleAuthError, KeyError, ValueError) as e:
        return fail("failed", f"{type(e).__name__}: {e}"[:300])

    if claims.get("nonce") != saved["nonce"]:
        return fail("failed", "ID token nonce mismatch")
    if not claims.get("email") or not claims.get("email_verified"):
        return fail("unverified", "Google account email is not verified")

    email = claims["email"].lower()
    hosted_domain = claims.get("hd")
    role = role_for_email(email, hosted_domain, settings.user_roles)
    record.user, record.role = email, role
    # Signed in either way: people without a role see an "access requested" page,
    # and this record tells the admin who is waiting.
    record.outcome = "signed_in" if role else "access_requested"
    audit.write(record)

    session = {"email": email, "name": claims.get("name") or email}
    if hosted_domain:
        session["hd"] = hosted_domain
    response = _back_to_app()
    _set_cookie(
        response,
        SESSION_COOKIE,
        sign(session, SESSION_PURPOSE, secret, settings.session_max_age_seconds),
        settings.session_max_age_seconds,
        settings,
    )
    response.delete_cookie(STATE_COOKIE, path="/auth")
    return response


@router.post("/logout")
async def logout(request: Request, settings: Annotated[Settings, Depends(get_settings)]):
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing {CSRF_HEADER} header")
    response = JSONResponse({"signed_out": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
