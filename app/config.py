from functools import lru_cache

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class KeyOwner(BaseModel):
    user: str
    role: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GCP project that holds the analytics datasets.
    gcp_project: str = "my-gcp-project"

    # sha256(api_key) hex digest -> owner. Raw keys are never stored.
    api_keys: dict[str, KeyOwner] = {}

    # role -> datasets that role may query.
    role_datasets: dict[str, list[str]] = {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
