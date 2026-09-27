"""Plans, usage metering and plan limits.

Every /ask is metered: tokens and bytes are priced into a cost, and the amount
charged depends on the person's plan:

  subscription  flat monthly fee; questions cost nothing extra until the month's
                included usage (at cost) is used up
  payg          each question is charged at cost x markup, up to a monthly cap
  freemium      free trial for people who aren't enrolled: capped per account per
                day, per IP address per day (so extra accounts don't multiply the
                allowance), and optionally in length

Usage is kept in SQLite so it survives restarts. That fits one server instance;
with several instances (e.g. Cloud Run scaling out), point the store at a shared
database instead (Cloud SQL, Firestore).
"""
import calendar
import ipaddress
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings

PLAN_NAMES = {"subscription": "Subscription", "payg": "Pay as you go", "freemium": "Free trial"}


# ---------- pricing ----------


@dataclass
class Cost:
    llm_usd: float
    bigquery_usd: float

    @property
    def total_usd(self) -> float:
        return self.llm_usd + self.bigquery_usd


def price(
    settings: Settings,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    bytes_processed: int = 0,
) -> Cost:
    """What a question cost us, at list prices (an estimate, not an invoice)."""
    llm = (
        input_tokens * settings.price_input_per_mtok
        + cache_read_tokens * settings.price_cache_read_per_mtok
        + cache_write_tokens * settings.price_cache_write_per_mtok
        + output_tokens * settings.price_output_per_mtok
    ) / 1_000_000
    bigquery = bytes_processed / 2**40 * settings.price_bigquery_per_tib
    return Cost(llm, bigquery)


def charge(plan: str, cost_usd: float, settings: Settings) -> float:
    """What the person pays for one question on their plan."""
    return cost_usd * settings.payg_markup if plan == "payg" else 0.0


# ---------- who is on which plan ----------


def _matches(email: str, entries: list[str]) -> bool:
    email = email.lower()
    domain = "@" + email.rsplit("@", 1)[-1] if "@" in email else None
    return any(e.lower() == email or (domain and e.lower() == domain) for e in entries)


def plan_for(user: str, settings: Settings) -> str:
    """Plan for an enrolled person: USER_PLANS entry (exact, then @domain), else the default."""
    key = user.lower()
    for entry, plan in settings.user_plans.items():
        if entry.lower() == key:
            return plan
    if "@" in key:
        return settings.user_plans.get("@" + key.rsplit("@", 1)[-1], settings.default_plan)
    return settings.default_plan


def client_ip(request: Request, settings: Settings) -> str:
    """The caller's IP. X-Forwarded-For is only trusted for the configured proxy hops."""
    if settings.trusted_proxy_hops > 0:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if len(hops) >= settings.trusted_proxy_hops:
            return hops[-settings.trusted_proxy_hops]
    return request.client.host if request.client else "unknown"


def _in_networks(ip: str, cidrs: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(c, strict=False) for c in cidrs)


def trial_refusal(email: str, ip: str, settings: Settings) -> str | None:
    """Why this account or network can't use the free trial, or None if it can."""
    if not settings.freemium_enabled or not settings.freemium_datasets:
        return "Free trials aren't available. Ask an admin to add you."
    if _matches(email, settings.freemium_blocked_accounts):
        return "Free trials aren't available for this account. Ask an admin to add you."
    if settings.freemium_allowed_accounts and not _matches(email, settings.freemium_allowed_accounts):
        return "Free trials are only open to invited accounts. Ask an admin to add you."
    if _in_networks(ip, settings.freemium_blocked_ips):
        return "Free trials aren't available from your network."
    if settings.freemium_allowed_ips and not _in_networks(ip, settings.freemium_allowed_ips):
        return "Free trials are only available from approved networks."
    return None


# ---------- storage ----------


@dataclass
class UsageRow:
    request_id: str
    user: str
    plan: str
    ip: str
    question: str
    status: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    bytes_processed: int = 0
    llm_cost_usd: float = 0.0
    bigquery_cost_usd: float = 0.0
    cost_usd: float = 0.0
    charged_usd: float = 0.0
    ts: str = ""  # ISO 8601 UTC; set on record


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UsageStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # A single connection keeps ":memory:" stores alive for tests; the lock
        # serialises access across FastAPI's worker threads.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage (
                  id INTEGER PRIMARY KEY,
                  ts TEXT NOT NULL,
                  day TEXT NOT NULL,
                  month TEXT NOT NULL,
                  request_id TEXT NOT NULL,
                  user TEXT NOT NULL,
                  plan TEXT NOT NULL,
                  ip TEXT NOT NULL,
                  question TEXT NOT NULL,
                  status TEXT NOT NULL,
                  input_tokens INTEGER NOT NULL,
                  cache_read_tokens INTEGER NOT NULL,
                  cache_write_tokens INTEGER NOT NULL,
                  output_tokens INTEGER NOT NULL,
                  bytes_processed INTEGER NOT NULL,
                  llm_cost_usd REAL NOT NULL,
                  bigquery_cost_usd REAL NOT NULL,
                  cost_usd REAL NOT NULL,
                  charged_usd REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_user_day ON usage (user, day);
                CREATE INDEX IF NOT EXISTS usage_user_month ON usage (user, month);
                CREATE INDEX IF NOT EXISTS usage_ip_day ON usage (ip, day, plan);
                """
            )

    def record(self, row: UsageRow, now: datetime | None = None) -> None:
        now = now or utc_now()
        row.ts = now.isoformat(timespec="seconds")
        values = asdict(row)
        values.update(day=now.date().isoformat(), month=now.strftime("%Y-%m"))
        columns = ", ".join(values)
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO usage ({columns}) VALUES ({', '.join(':' + c for c in values)})", values
            )

    def _one(self, sql: str, *args) -> sqlite3.Row:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _all(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def day_totals(self, user: str, day: str, plan: str | None = None) -> tuple[int, float]:
        r = self._one(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE user = ? AND day = ? AND (? IS NULL OR plan = ?)",
            user, day, plan, plan,
        )
        return r[0], r[1]

    def day_status_count(self, user: str, day: str, status: str) -> int:
        return self._one(
            "SELECT COUNT(*) FROM usage WHERE user = ? AND day = ? AND status = ?", user, day, status
        )[0]

    def ip_trial_questions(self, ip: str, day: str) -> int:
        return self._one(
            "SELECT COUNT(*) FROM usage WHERE ip = ? AND day = ? AND plan = 'freemium'", ip, day
        )[0]

    def month_totals(self, user: str, month: str, plan: str | None = None) -> dict:
        r = self._one(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(charged_usd), 0) "
            "FROM usage WHERE user = ? AND month = ? AND (? IS NULL OR plan = ?)",
            user, month, plan, plan,
        )
        return {"questions": r[0], "cost_usd": r[1], "charged_usd": r[2]}

    def first_use(self, user: str, plan: str) -> str | None:
        return self._one("SELECT MIN(ts) FROM usage WHERE user = ? AND plan = ?", user, plan)[0]

    def daily(self, user: str, month: str) -> dict[str, dict]:
        rows = self._all(
            "SELECT day, COUNT(*) AS questions, SUM(cost_usd) AS cost, SUM(charged_usd) AS charged "
            "FROM usage WHERE user = ? AND month = ? GROUP BY day",
            user,
            month,
        )
        return {r["day"]: {"questions": r["questions"], "cost_usd": r["cost"], "charged_usd": r["charged"]} for r in rows}

    def recent(self, user: str, limit: int = 20) -> list[dict]:
        rows = self._all(
            "SELECT ts, question, status, cost_usd, charged_usd FROM usage "
            "WHERE user = ? ORDER BY id DESC LIMIT ?",
            user,
            limit,
        )
        return [dict(r) for r in rows]

    def all_users(self, month: str) -> list[dict]:
        rows = self._all(
            "SELECT user, plan, COUNT(*) AS questions, SUM(cost_usd) AS cost_usd, "
            "SUM(charged_usd) AS charged_usd, MAX(ts) AS last_used "
            "FROM usage WHERE month = ? GROUP BY user, plan ORDER BY cost_usd DESC",
            month,
        )
        return [dict(r) for r in rows]


@lru_cache
def _usage_store(path: str) -> UsageStore:
    return UsageStore(path)


def get_usage_store(settings: Annotated[Settings, Depends(get_settings)]) -> UsageStore:
    return _usage_store(settings.usage_db_path)


# ---------- limits ----------


def next_month_start(now: datetime) -> datetime:
    days = calendar.monthrange(now.year, now.month)[1]
    return (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days))


def trial_ends(user: str, store: UsageStore, settings: Settings) -> datetime | None:
    if not settings.freemium_trial_days:
        return None
    first = store.first_use(user, "freemium")
    if first is None:
        return None
    return datetime.fromisoformat(first) + timedelta(days=settings.freemium_trial_days)


def limits(user: str, plan: str, ip: str | None, store: UsageStore, settings: Settings, now: datetime) -> list[dict]:
    """The caps that apply to this person right now, with what they've used.

    Each cap counts only usage made on that plan, so moving someone from the
    trial to a subscription (or back) starts the new plan's allowance fresh.
    """
    today, month = now.date().isoformat(), now.strftime("%Y-%m")
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    renews = next_month_start(now)
    out = []
    if plan == "freemium":
        questions, cost = store.day_totals(user, today, plan)
        out.append({
            "id": "trial_questions", "label": "Free questions today", "unit": "questions",
            "used": questions, "limit": settings.freemium_daily_questions, "resets_at": tomorrow.isoformat(),
        })
        out.append({
            "id": "trial_cost", "label": "Free usage today", "unit": "usd",
            "used": cost, "limit": settings.freemium_daily_cost_usd, "resets_at": tomorrow.isoformat(),
        })
        if ip is not None:
            out.append({
                "id": "trial_ip", "label": "Free questions from your network today", "unit": "questions",
                "used": store.ip_trial_questions(ip, today), "limit": settings.freemium_ip_daily_questions,
                "resets_at": tomorrow.isoformat(), "private": True,
            })
    elif plan == "subscription":
        m = store.month_totals(user, month, plan)
        out.append({
            "id": "included_usage", "label": "Included usage this month", "unit": "usd",
            "used": m["cost_usd"], "limit": settings.subscription_monthly_allowance_usd,
            "resets_at": renews.isoformat(),
        })
    elif plan == "payg" and settings.payg_monthly_limit_usd is not None:
        m = store.month_totals(user, month, plan)
        out.append({
            "id": "spend_cap", "label": "Spending this month", "unit": "usd",
            "used": m["charged_usd"], "limit": settings.payg_monthly_limit_usd,
            "resets_at": renews.isoformat(),
        })
    return out


LIMIT_MESSAGES = {
    "trial_questions": "You've used today's {limit:g} free questions. They reset at midnight UTC.",
    "trial_cost": "You've used today's free allowance. It resets at midnight UTC.",
    "trial_ip": "Free questions from your network are used up for today. They reset at midnight UTC.",
    "included_usage": "You've used this month's included usage. It renews on {renews}.",
    "spend_cap": "You've reached your monthly spending limit of ${limit:,.2f}. It resets on {renews}.",
}


def enforce(user: str, plan: str, ip: str, store: UsageStore, settings: Settings, now: datetime | None = None) -> None:
    """Refuse a new question (402) when a plan limit is used up.

    Checked before the question runs, so one in-flight question can take usage
    slightly past a cap; the per-minute rate limit bounds how far.
    """
    now = now or utc_now()
    if plan == "freemium":
        ends = trial_ends(user, store, settings)
        if ends is not None and now >= ends:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"Your free trial ended on {ends:%d %b %Y}. Ask an admin to add you to keep going.",
            )
    for item in limits(user, plan, ip, store, settings, now):
        if item["used"] >= item["limit"]:
            renews = datetime.fromisoformat(item["resets_at"])
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                LIMIT_MESSAGES[item["id"]].format(limit=item["limit"], renews=f"{renews:%d %b %Y}"),
            )
