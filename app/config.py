from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


# subscription: flat monthly fee with included usage; payg: charged per question;
# freemium: free trial with daily caps.
Plan = Literal["subscription", "payg", "freemium"]


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

    # Google sign-in for people (API keys remain for scripts and integrations).
    # Enabled when the client ID, client secret and session secret are all set.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Signs session cookies; any long random string (python -c "import secrets; print(secrets.token_urlsafe(48))").
    session_secret: SecretStr | None = None
    # Who may use the app after signing in with Google, and with which role:
    # an exact address ("ana@company.com") or a whole Google Workspace domain
    # ("@company.com", matched on Google's verified hosted-domain claim).
    user_roles: dict[str, str] = {}
    # Must match an authorized redirect URI on the OAuth client. Leave unset to
    # derive it from the request (<scheme>://<host>/auth/callback).
    oauth_redirect_uri: str | None = None
    session_max_age_seconds: int = 8 * 3600
    # Send cookies over HTTPS only. Set false only for local http development.
    session_cookie_secure: bool = True

    # ---- Plans, usage metering and limits ----
    # Where metered usage is stored (SQLite). One row per question.
    usage_db_path: str = "data/usage.sqlite3"
    # Plan per person: exact email / API-key user name, or "@domain".
    user_plans: dict[str, Plan] = {}
    # Plan for enrolled people (USER_ROLES / API_KEYS) with no USER_PLANS entry.
    default_plan: Plan = "subscription"
    # Roles that can see everyone's usage on the usage page.
    usage_admin_roles: list[str] = ["admin"]

    # Prices in USD, used to cost each question. Claude prices are per million
    # tokens for AGENT_MODEL (claude-opus-5 list prices); BigQuery on-demand per TiB.
    price_input_per_mtok: float = 5.00
    price_output_per_mtok: float = 25.00
    price_cache_read_per_mtok: float = 0.50
    price_cache_write_per_mtok: float = 6.25
    price_bigquery_per_tib: float = 6.25

    # Subscription: a flat monthly fee that includes this much usage (at cost).
    subscription_fee_usd: float = 20.00
    subscription_monthly_allowance_usd: float = 25.00
    # Pay as you go: each question is charged at cost x markup, up to a monthly cap.
    payg_markup: float = 1.20
    payg_monthly_limit_usd: float | None = 100.00

    # Freemium: people who sign in with Google but aren't enrolled get a free trial
    # on these datasets (keep company data out of it), with daily caps.
    freemium_enabled: bool = False
    freemium_datasets: list[str] = ["ga4"]
    freemium_daily_questions: int = 5
    freemium_daily_cost_usd: float = 0.50
    # Shared by every trial account from one IP address, so extra Google accounts
    # don't multiply the free allowance.
    freemium_ip_daily_questions: int = 15
    # Trial length from the first question; unset for no end date.
    freemium_trial_days: int | None = 14
    # Who may start a trial: emails or "@domain" (empty = anyone), and who may not.
    freemium_allowed_accounts: list[str] = []
    freemium_blocked_accounts: list[str] = []
    # Networks (CIDR, e.g. "203.0.113.0/24") allowed to use trials (empty = any) or blocked.
    freemium_allowed_ips: list[str] = []
    freemium_blocked_ips: list[str] = []
    # Proxies in front of the app that append to X-Forwarded-For (Cloud Run: 1).
    # 0 uses the connecting address and ignores the header, which clients can forge.
    trusted_proxy_hops: int = 0

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.session_secret)

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

    # Read from .env; the SDK itself only sees real environment variables.
    # When unset, the SDK falls back to its own credential resolution.
    anthropic_api_key: SecretStr | None = None

    # Claude model and effort used by the analytics agent.
    agent_model: str = "claude-opus-5"
    agent_effort: str = "high"

    # Rejected execute_query calls allowed before the agent must explain instead.
    agent_max_query_retries: int = 3

    # Hard cap on model turns per question.
    agent_max_turns: int = 12

    # Per-user limits on /ask (in memory, per server instance).
    ask_rate_limit_per_minute: int = 10
    user_daily_bytes_limit: int = 10 * 1024**3

    # Seconds before /ask gives up and returns 504.
    ask_timeout_seconds: float = 180.0

    # Where /ask audit records go: a JSON-lines file, or "-" for stdout (Cloud Run).
    audit_log_path: str = "logs/audit.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()
