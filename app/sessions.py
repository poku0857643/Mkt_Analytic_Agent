"""Signed, expiring tokens for cookies (the browser session and the OAuth state).

A token is base64url(JSON payload) + "." + base64url(HMAC-SHA256). The payload
is readable but cannot be changed without the secret. Each token carries a
purpose ("typ") so a token issued for one cookie is never accepted as another.
"""
import base64
import hashlib
import hmac
import json
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def sign(data: dict, purpose: str, secret: str, max_age: int, now: float | None = None) -> str:
    payload = {**data, "typ": purpose, "exp": int((now or time.time()) + max_age)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_mac(body, secret)}"


def verify(token: str | None, purpose: str, secret: str, now: float | None = None) -> dict | None:
    """The payload if the token is authentic, unexpired and for this purpose, else None."""
    if not token or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    if not hmac.compare_digest(mac, _mac(body, secret)):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("typ") != purpose:
        return None
    if not isinstance(payload.get("exp"), int) or payload["exp"] < (now or time.time()):
        return None
    return payload
