import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.auth import hash_key
from app.config import KeyOwner, Settings, get_settings
from app.usage import UsageStore, get_usage_store

# Raw keys the tests send; test_auth.py uses the same values.
ANALYST_KEY = "analyst-test-key"
ADMIN_KEY = "admin-test-key"
NO_ACCESS_KEY = "intern-test-key"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        gcp_project="test-project",
        api_keys={
            hash_key(ANALYST_KEY): KeyOwner(user="ana", role="analyst"),
            hash_key(ADMIN_KEY): KeyOwner(user="adam", role="admin"),
            hash_key(NO_ACCESS_KEY): KeyOwner(user="ivy", role="intern"),
        },
        role_datasets={
            "analyst": ["marketing"],
            "admin": ["marketing", "customers"],
        },
        pii_columns=["customers.customers.email", "customers.customers.phone"],
    )


@pytest.fixture
def usage_store():
    return UsageStore(":memory:")


@pytest.fixture
def client(settings, usage_store):
    app.dependency_overrides[get_settings] = lambda: settings
    # In memory, so tests never write a usage database to disk.
    app.dependency_overrides[get_usage_store] = lambda: usage_store
    yield TestClient(app)
    app.dependency_overrides.clear()