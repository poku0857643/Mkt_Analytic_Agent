import asyncio
import math
import time
import uuid
from functools import lru_cache
from typing import Annotated

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Security, status
from google.cloud import bigquery
from pydantic import BaseModel, Field

from app.agent import AgentResult, AnalyticsAgent, Chart, build_agent
from app.audit import AuditLog, AuditQuery, AuditRecord, JsonLinesAuditLog
from app.auth import User, api_key_header, get_current_user, hash_key
from app.bigquery_tools import BigQueryTools
from app.config import Settings, get_settings
from app.limits import DailyScanBudget, RateLimiter
from app.mcp_server import build_server

app = FastAPI(title="Marketing Analytics Agent")


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
def _audit_log(path: str) -> JsonLinesAuditLog:
    return JsonLinesAuditLog(path)


def get_audit_log(settings: Annotated[Settings, Depends(get_settings)]) -> AuditLog:
    return _audit_log(settings.audit_log_path)


@lru_cache
def _rate_limiter(limit: int) -> RateLimiter:
    return RateLimiter(limit, window=60.0)


def get_rate_limiter(settings: Annotated[Settings, Depends(get_settings)]) -> RateLimiter:
    return _rate_limiter(settings.ask_rate_limit_per_minute)


@lru_cache
def _scan_budget(limit_bytes: int) -> DailyScanBudget:
    return DailyScanBudget(limit_bytes)


def get_scan_budget(settings: Annotated[Settings, Depends(get_settings)]) -> DailyScanBudget:
    return _scan_budget(settings.user_daily_bytes_limit)


# Routes


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
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditLog, Depends(get_audit_log)],
) -> User:
    """get_current_user, but denied attempts are written to the audit log."""
    try:
        return get_current_user(api_key, settings)
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
) -> AskResponse:
    started = time.monotonic()
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
        # Charge every byte scanned, including queries run before a failure.
        budget.add(user.user, tools.bytes_processed)
        record.total_bytes_processed = tools.bytes_processed
        record.duration_ms = int((time.monotonic() - started) * 1000)
        audit.write(record)

    return AskResponse(
        request_id=record.request_id,
        status=result.answer.status,
        summary=result.answer.summary,
        chart=result.answer.chart,
        sql_used=result.sql_used,
        bytes_processed=record.total_bytes_processed,
    )
