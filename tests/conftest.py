import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.config import Settings, get_settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, gcp_project="test-project")


@pytest.fixture
def client(settings):
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()
