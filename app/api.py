import asyncio
import math
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.cloud import bigquery
from pydantic import BaseModel, Field

from app.agent import AgentResult, AnalyticsAgent, Chart, build_agent
from app.audit import AuditLog, AuditQuery, AuditRecord, get_audit_log
from app.auth import User, api_key_header, get_current_user, hash_key
from app.bigquery_tools import BigQueryTools
from app.config import Settings, get_settings
from app.google_auth import router as google_auth_router
from app.limits import DailyScanBudget, InFlightLimiter, RateLimiter
from app.mcp_server import build_server
from app.usage import (
    PLAN_NAMES,
    SELF_SERVE_PLANS,
    UsageRow,
    UsageStore,
    cancel_plan,
    charge,
    choose_plan,
    client_ip,
    enforce,
    get_usage_store,
    limits,
    next_month_start,
    plan_status,
    price,
    trial_ends,
    utc_now,
)

app = FastAPI(title="Marketing Analytics Agent")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# The bundled chart library is ~1.1 MB; compressed it is about a third of that.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# The web UI loads nothing from other origins. /docs (Swagger) is left alone
# because it pulls its assets from a CDN.
UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Question text can appear in UI links (?q=); don't leak it to other sites.
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Content-Security-Policy"] = UI_CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-cache"
    return response


# Shared clients, built once per process. Tests override these dependencies.


@lru_cache
def _bigquery_client(project: str) -> bigquery.Client:
    return bigquery.Client(project=project)


def get_bigquery_client(settings: Annotated[Settings, Depends(get_settings)]) -> bigquery.Client:
    return _bigquery_client(settings.gcp_project)


_agent: AnalyticsAgent | None = None


def get_agent(settings: Annotated[Settings, Depends(get_settings)]) -> AnalyticsAgent:
    global _agent
    if _agent is None:
        _agent = build_agent(settings)
    return _agent


@lru_cache
def _rate_limiter(limit: int) -> RateLimiter:
    return RateLimiter(limit, window=60.0)


def get_rate_limiter(settings: Annotated[Settings, Depends(get_settings)]) -> RateLimiter:
    return _rate_limiter(settings.ask_rate_limit_per_minute)


@lru_cache
def _inflight_limiter(limit: int) -> InFlightLimiter:
    return InFlightLimiter(limit)


def get_inflight_limiter(settings: Annotated[Settings, Depends(get_settings)]) -> InFlightLimiter:
    return _inflight_limiter(settings.ask_max_concurrent_per_user)


@lru_cache
def _scan_budget(limit_bytes: int) -> DailyScanBudget:
    return DailyScanBudget(limit_bytes)


def get_scan_budget(settings: Annotated[Settings, Depends(get_settings)]) -> DailyScanBudget:
    return _scan_budget(settings.user_daily_bytes_limit)


# Routes

app.include_router(google_auth_router)


@app.get("/", include_in_schema=False)
async def web_ui():
    """Browser UI for non-technical users; it calls /whoami and /ask."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/whoami")
async def whoami(user: Annotated[User, Depends(get_current_user)]) -> User:
    """Check which user, role and datasets an API key maps to."""
    return user


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class AskResponse(BaseModel):
    request_id: str
    status: str
    summary: str
    chart: Chart | None
    sql_used: list[str]
    bytes_processed: int


def audited_user(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditLog, Depends(get_audit_log)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
) -> User:
    """get_current_user, but denied attempts are written to the audit log."""
    try:
        return get_current_user(request, api_key, settings, usage)
    except HTTPException as e:
        audit.write(
            AuditRecord(
                request_id=str(uuid.uuid4()),
                outcome="denied",
                http_status=e.status_code,
                key_hash_prefix=hash_key(api_key)[:8] if api_key else None,
                error=str(e.detail),
            )
        )
        raise


def _meter(
    usage: UsageStore,
    settings: Settings,
    user: User,
    ip: str,
    question: str,
    record: AuditRecord,
    result: AgentResult | None,
) -> None:
    """Record what the question cost. Failed questions still count what they used."""
    # Token counts come from the agent result; a question that failed mid-way
    # only has its BigQuery bytes (the agent's partial token use isn't returned).
    tokens = dict(
        input_tokens=result.input_tokens if result else 0,
        cache_read_tokens=result.cache_read_tokens if result else 0,
        cache_write_tokens=result.cache_write_tokens if result else 0,
        output_tokens=result.output_tokens if result else 0,
    )
    cost = price(settings, bytes_processed=record.total_bytes_processed, **tokens)
    usage.record(
        UsageRow(
            request_id=record.request_id,
            user=user.user,
            plan=user.plan,
            ip=ip,
            question=question,
            status=record.outcome,
            bytes_processed=record.total_bytes_processed,
            llm_cost_usd=cost.llm_usd,
            bigquery_cost_usd=cost.bigquery_usd,
            cost_usd=cost.total_usd,
            charged_usd=charge(user.plan, cost.total_usd, settings),
            **tokens,
        )
    )


def _audit_from_result(record: AuditRecord, result: AgentResult) -> None:
    record.queries = [
        AuditQuery(q.sql, q.approved, q.detail, q.bytes_processed) for q in result.queries
    ]
    record.turns = result.turns
    record.input_tokens = result.input_tokens
    record.cache_read_tokens = result.cache_read_tokens
    record.cache_write_tokens = result.cache_write_tokens
    record.output_tokens = result.output_tokens


@app.post("/ask")
async def ask(
    body: AskRequest,
    user: Annotated[User, Depends(audited_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    bq: Annotated[bigquery.Client, Depends(get_bigquery_client)],
    agent: Annotated[AnalyticsAgent, Depends(get_agent)],
    audit: Annotated[AuditLog, Depends(get_audit_log)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    budget: Annotated[DailyScanBudget, Depends(get_scan_budget)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
    inflight: Annotated[InFlightLimiter, Depends(get_inflight_limiter)],
    request: Request,
) -> AskResponse:
    started = time.monotonic()
    ip = client_ip(request, settings)
    record = AuditRecord(
        request_id=str(uuid.uuid4()),
        outcome="error",
        http_status=500,
        user=user.user,
        role=user.role,
        question=body.question,
    )

    retry_after = limiter.check(user.user)
    if retry_after is not None:
        record.outcome, record.http_status = "rate_limited", status.HTTP_429_TOO_MANY_REQUESTS
        audit.write(record)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many questions. Please wait a moment.",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )

    # Anti-abuse and anti-distillation: cap daily volume on every plan, and pause
    # people who keep sending out-of-scope requests (probing, prompt extraction).
    today = utc_now().date().isoformat()
    if settings.ask_daily_question_limit is not None and (
        usage.day_totals(user.user, today)[0] >= settings.ask_daily_question_limit
    ):
        record.outcome, record.http_status = "daily_limit", status.HTTP_429_TOO_MANY_REQUESTS
        audit.write(record)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"You've asked {settings.ask_daily_question_limit} questions today, the daily maximum. "
            "It resets at midnight UTC.",
        )
    if usage.day_status_count(user.user, today, "declined") >= settings.declined_daily_limit:
        record.outcome, record.http_status = "paused", status.HTTP_429_TOO_MANY_REQUESTS
        audit.write(record)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Questions are paused until midnight UTC: too many requests today were outside what "
            "this assistant covers. It only answers questions about the marketing data.",
        )

    try:
        enforce(user.user, user.plan, ip, usage, settings)
    except HTTPException:
        record.outcome, record.http_status = "plan_limit", status.HTTP_402_PAYMENT_REQUIRED
        audit.write(record)
        raise

    remaining = budget.remaining(user.user)
    if remaining <= 0:
        record.outcome, record.http_status = "over_budget", status.HTTP_429_TOO_MANY_REQUESTS
        audit.write(record)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Daily data scan limit reached. It resets at 00:00 UTC.",
        )

    # The allowlist comes from the caller's role, never from the request.
    # A single query may not scan more than the user has left today.
    tools = BigQueryTools(
        client=bq,
        project=settings.gcp_project,
        allowed_datasets=user.allowed_datasets,
        pii_columns=settings.pii_columns,
        max_bytes=min(settings.max_bytes_billed, remaining),
        max_rows=settings.max_result_rows,
        timeout=settings.query_timeout_seconds,
    )

    if not inflight.try_acquire(user.user):
        record.outcome, record.http_status = "rate_limited", status.HTTP_429_TOO_MANY_REQUESTS
        record.error = "question already running"
        audit.write(record)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "You already have a question running. Wait for it to finish, then ask again.",
        )

    result: AgentResult | None = None
    try:
        async with asyncio.timeout(settings.ask_timeout_seconds):
            result = await agent.ask(body.question, build_server(tools), user.allowed_datasets)
    except TimeoutError as e:
        record.http_status = status.HTTP_504_GATEWAY_TIMEOUT
        record.error = f"Timed out after {settings.ask_timeout_seconds:g}s"
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, "The question took too long. Try a narrower question."
        ) from e
    except anthropic.APIError as e:
        record.http_status = status.HTTP_502_BAD_GATEWAY
        record.error = f"{type(e).__name__}: {e}"[:500]
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The AI service is unavailable. Try again shortly."
        ) from e
    except Exception as e:
        record.error = f"{type(e).__name__}: {e}"[:500]
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "The question could not be processed."
        ) from e
    else:
        _audit_from_result(record, result)
        record.outcome = result.answer.status
        record.http_status = status.HTTP_200_OK
    finally:
        try:
            # Charge every byte scanned, including queries run before a failure.
            budget.add(user.user, tools.bytes_processed)
            record.total_bytes_processed = tools.bytes_processed
            record.duration_ms = int((time.monotonic() - started) * 1000)
            audit.write(record)
            _meter(usage, settings, user, ip, body.question, record, result)
        finally:
            inflight.release(user.user)

    return AskResponse(
        request_id=record.request_id,
        status=result.answer.status,
        summary=result.answer.summary,
        chart=result.answer.chart,
        sql_used=result.sql_used,
        bytes_processed=record.total_bytes_processed,
    )


# Usage page


def _plan_info(plan: str, settings: Settings) -> dict:
    if plan == "subscription":
        summary = (
            f"${settings.subscription_fee_usd:,.2f} a month, including "
            f"${settings.subscription_monthly_allowance_usd:,.2f} of usage."
        )
    elif plan == "payg":
        cap = settings.payg_monthly_limit_usd
        summary = f"Charged per question at cost \u00d7 {settings.payg_markup:g}" + (
            f", up to ${cap:,.2f} a month." if cap is not None else "."
        )
    elif plan == "none":
        summary = "You don't have an active plan. Choose one to keep asking questions."
    else:
        summary = (
            f"Free: up to {settings.freemium_daily_questions} questions a day"
            + (f" for {settings.freemium_trial_days} days" if settings.freemium_trial_days else "")
            + ", on sample data."
        )
    return {"id": plan, "name": PLAN_NAMES.get(plan, plan), "summary": summary}


@app.get("/usage")
async def usage_report(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
):
    """The caller's plan, limits, and usage this month."""
    now = utc_now()
    month = now.strftime("%Y-%m")
    by_day = usage.daily(user.user, month)
    days = [
        {"day": d, **by_day.get(d, {"questions": 0, "cost_usd": 0.0, "charged_usd": 0.0})}
        for d in (f"{month}-{n:02d}" for n in range(1, now.day + 1))
    ]
    today = usage.day_totals(user.user, now.date().isoformat())
    ends = trial_ends(user.user, usage, settings) if user.plan == "freemium" else None
    return {
        "user": user.user,
        "plan": {**_plan_info(user.plan, settings), **plan_status(user.user, user.plan, usage, now)},
        # Enrolled people choose their own plan; trial accounts are enrolled by an admin.
        "can_change_plan": user.role != "trial",
        "plan_options": [_plan_info(p, settings) for p in SELF_SERVE_PLANS],
        "period": {"month": month, "renews_on": next_month_start(now).date().isoformat()},
        "today": {"questions": today[0], "cost_usd": round(today[1], 4)},
        "month": {k: round(v, 4) if isinstance(v, float) else v for k, v in usage.month_totals(user.user, month).items()},
        # Per-network trial counts include other people, so they aren't shown.
        "limits": [
            {**item, "used": round(item["used"], 4)}
            for item in limits(user.user, user.plan, client_ip(request, settings), usage, settings, now)
            if not item.get("private")
        ],
        "trial_ends_on": ends.date().isoformat() if ends else None,
        "daily": days,
        "recent": usage.recent(user.user),
        "is_usage_admin": user.role in settings.usage_admin_roles,
    }


@app.get("/usage/all")
async def usage_all_users(
    user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
):
    """Everyone's usage this month, for admins."""
    if user.role not in settings.usage_admin_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can see everyone's usage")
    month = utc_now().strftime("%Y-%m")
    return {"month": month, "users": usage.all_users(month)}


# Changing plans


class PlanChoice(BaseModel):
    plan: Literal["subscription", "payg"]


def _plan_owner(user: User) -> User:
    if user.role == "trial":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Free trial accounts can't choose a plan. Ask an admin to add you.",
        )
    return user


def _audit_plan(audit: AuditLog, user: User, detail: str) -> None:
    audit.write(
        AuditRecord(
            request_id=str(uuid.uuid4()),
            event="plan_change",
            outcome="plan_changed",
            http_status=status.HTTP_200_OK,
            user=user.user,
            role=user.role,
            error=detail,  # what changed, e.g. "subscription -> payg"
        )
    )


@app.post("/plan")
async def change_plan(
    body: PlanChoice,
    user: Annotated[User, Depends(get_current_user)],
    audit: Annotated[AuditLog, Depends(get_audit_log)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
):
    """Switch to Subscription or Pay as you go, starting now (undoes a pending cancellation)."""
    _plan_owner(user)
    now = utc_now()
    choose_plan(user.user, body.plan, usage, now)
    _audit_plan(audit, user, f"{user.plan} -> {body.plan}")
    return {"plan": body.plan, **plan_status(user.user, body.plan, usage, now)}


@app.post("/plan/cancel")
async def cancel_current_plan(
    user: Annotated[User, Depends(get_current_user)],
    audit: Annotated[AuditLog, Depends(get_audit_log)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
):
    """Quit: a subscription runs to the end of the month, pay as you go stops now."""
    _plan_owner(user)
    now = utc_now()
    if plan_status(user.user, user.plan, usage, now)["state"] == "ending":
        raise HTTPException(status.HTTP_409_CONFLICT, "Your subscription is already cancelled.")
    ends_on = cancel_plan(user.user, user.plan, usage, now)
    _audit_plan(audit, user, f"cancel {user.plan}" + (f", ends {ends_on}" if ends_on else ", ended now"))
    plan = user.plan if ends_on else "none"
    return {"plan": plan, **plan_status(user.user, plan, usage, now)}


@app.post("/plan/resume")
async def resume_subscription(
    user: Annotated[User, Depends(get_current_user)],
    audit: Annotated[AuditLog, Depends(get_audit_log)],
    usage: Annotated[UsageStore, Depends(get_usage_store)],
):
    """Keep a cancelled subscription that hasn't ended yet."""
    _plan_owner(user)
    now = utc_now()
    if plan_status(user.user, user.plan, usage, now)["state"] != "ending":
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no cancellation to undo.")
    choose_plan(user.user, "subscription", usage, now)
    _audit_plan(audit, user, "resume subscription")
    return {"plan": "subscription", **plan_status(user.user, "subscription", usage, now)}
