import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.config import KeyOwner, Settings, get_settings
from app.sessions import verify
from app.usage import UsageStore, client_ip, effective_plan, get_usage_store, trial_refusal

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

SESSION_COOKIE = "mkt_session"
SESSION_PURPOSE = "session"
# Browsers only send this header from our own page's fetch() calls: a cross-site
# form or link can't set it, and a cross-site fetch() that sets it needs a CORS
# preflight, which this API never grants. Required for cookie-authenticated writes.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "fetch"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class User(BaseModel):
    user: str
    role: str
    allowed_datasets: list[str]
    plan: str = "subscription"


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def lookup_key(api_key: str, api_keys: dict[str, KeyOwner]) -> KeyOwner | None:
    """Find the owner of an API key, comparing hashes in constant time."""
    digest = hash_key(api_key)
    owner = None
    for stored_digest, candidate in api_keys.items():
        # Check every entry so timing does not reveal which one matched.
        if hmac.compare_digest(digest, stored_digest):
            owner = candidate
    return owner


def role_for_email(email: str, hosted_domain: str | None, user_roles: dict[str, str]) -> str | None:
    """Role for a verified Google account: exact address first, then Workspace domain."""
    email = email.lower()
    for entry, role in user_roles.items():
        if entry.lower() == email:
            return role
    if hosted_domain:
        # "@company.com" matches only accounts Google says belong to that
        # Workspace domain (the hd claim), not any address ending in it.
        return user_roles.get("@" + hosted_domain.lower())
    return None


def read_session(request: Request, settings: Settings) -> dict | None:
    if not settings.session_secret:
        return None
    return verify(
        request.cookies.get(SESSION_COOKIE),
        SESSION_PURPOSE,
        settings.session_secret.get_secret_value(),
    )


def _with_datasets(user: str, role: str, settings: Settings, usage: UsageStore) -> User:
    datasets = settings.role_datasets.get(role, [])
    if not datasets:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Role '{role}' has no dataset access",
        )
    return User(user=user, role=role, allowed_datasets=datasets, plan=effective_plan(user, settings, usage))


def get_current_user(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
) -> User:
    """The caller, from an X-API-Key header (scripts) or a Google sign-in session (people)."""
    if api_key:
        owner = lookup_key(api_key, settings.api_keys)
        if owner is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid API key",
                headers={"WWW-Authenticate": "APIKey"},
            )
        return _with_datasets(owner.user, owner.role, settings, usage)

    session = read_session(request, settings)
    if session is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not signed in",
            headers={"WWW-Authenticate": "APIKey"},
        )
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing {CSRF_HEADER} header")

    email = session["email"]
    # Looked up on every request, so removing someone from USER_ROLES takes effect at once.
    role = role_for_email(email, session.get("hd"), settings.user_roles)
    if role is None:
        # Not enrolled: a free trial on the trial datasets, if this account and
        # network may have one.
        refusal = trial_refusal(email, client_ip(request, settings), settings)
        if refusal:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{email} does not have access yet. {refusal}",
            )
        return User(user=email, role="trial", allowed_datasets=list(settings.freemium_datasets), plan="freemium")
    return _with_datasets(email, role, settings, usage)
