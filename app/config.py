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

    # Fully qualified "dataset.table.column" names that must never be queried.
    pii_columns: list[str] = []

    # Upper bound on bytes a single query may scan (dry-run check and
    # maximum_bytes_billed on real jobs). Default 1 GiB.
    max_bytes_billed: int = 1024**3

    # Rows returned to the agent per query; keeps LLM context small.
    max_result_rows: int = 500

    # Seconds to wait for a query to finish.
    query_timeout_seconds: float = 60.0

    # Datasets for the standalone MCP server (python -m app.mcp_server).
    # Inside the API, each request uses the caller's role datasets instead.
    mcp_allowed_datasets: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
