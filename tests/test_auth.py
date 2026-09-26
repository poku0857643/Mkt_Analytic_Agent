from app.auth import hash_key, lookup_key
from app.config import KeyOwner

ANALYST_KEY = "analyst-test-key"
ADMIN_KEY = "admin-test-key"
NO_ACCESS_KEY = "intern-test-key"


def test_missing_key_is_401(client):
    response = client.get("/whoami")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "APIKey"


def test_wrong_key_is_401(client):
    response = client.get("/whoami", headers={"X-API-Key": "not-a-real-key"})
    assert response.status_code == 401


def test_valid_key_returns_user_and_datasets(client):
    response = client.get("/whoami", headers={"X-API-Key": ANALYST_KEY})
    assert response.status_code == 200
    assert response.json() == {
        "user": "ana",
        "role": "analyst",
        "allowed_datasets": ["marketing"],
    }


def test_admin_sees_more_datasets(client):
    response = client.get("/whoami", headers={"X-API-Key": ADMIN_KEY})
    assert response.status_code == 200
    assert response.json()["allowed_datasets"] == ["marketing", "customers"]


def test_role_without_datasets_is_403(client):
    response = client.get("/whoami", headers={"X-API-Key": NO_ACCESS_KEY})
    assert response.status_code == 403


def test_hash_key_is_sha256_hex():
    digest = hash_key("abc")
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_lookup_key():
    owner = KeyOwner(user="u", role="r")
    keys = {hash_key("k1"): owner}
    assert lookup_key("k1", keys) == owner
    assert lookup_key("k2", keys) is None
    assert lookup_key("k1", {}) is None