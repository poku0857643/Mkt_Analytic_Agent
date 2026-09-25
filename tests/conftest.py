import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.auth import hash_key
from app.config import KeyOwner, Settings, get_settings

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
    )


@pytest.fixture
def client(settings):
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()
