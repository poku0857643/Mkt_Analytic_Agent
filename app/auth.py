import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.config import KeyOwner, Settings, get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class User(BaseModel):
    user: str
    role: str
    allowed_datasets: list[str]


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


def get_current_user(
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    if not api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing API key",
            headers={"WWW-Authenticate": "APIKey"},
        )

    owner = lookup_key(api_key, settings.api_keys)
    if owner is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid API key",
            headers={"WWW-Authenticate": "APIKey"},
        )

    datasets = settings.role_datasets.get(owner.role, [])
    if not datasets:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Role '{owner.role}' has no dataset access",
        )

    return User(user=owner.user, role=owner.role, allowed_datasets=datasets)
