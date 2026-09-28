"""Audit log: one JSON record per /ask request (including denied and failed ones)
and per Google sign-in attempt.

Records go to a JSON-lines file, or to stdout when the path is "-". On Cloud Run,
stdout JSON lands in Cloud Logging, which can be routed to BigQuery with a log
sink, so the app itself never needs write access to BigQuery.

Never recorded: API keys (only a short hash prefix), tokens, cookies and answer text.
"""
import json
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Protocol

from fastapi import Depends

from app.config import Settings, get_settings

Outcome = Literal[
    "answered", "limitation", "declined", "denied", "rate_limited", "over_budget", "plan_limit",
    "daily_limit", "paused", "error",
    # Plan events: someone switched, cancelled or resumed their plan.
    "plan_changed",
    # Sign-in events: signed in with a role, or signed in and waiting for one.
    "signed_in", "access_requested",
]


@dataclass
class AuditQuery:
    sql: str
    approved: bool
    detail: str
    bytes_processed: int


@dataclass
class AuditRecord:
    request_id: str
    outcome: Outcome
    http_status: int
    event: Literal["ask", "sign_in", "plan_change"] = "ask"
    user: str | None = None
    role: str | None = None
    # First 8 hex chars of sha256(key) for denied requests, to spot misuse.
    key_hash_prefix: str | None = None
    question: str | None = None
    queries: list[AuditQuery] = field(default_factory=list)
    total_bytes_processed: int = 0
    turns: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )


class AuditLog(Protocol):
    def write(self, record: AuditRecord) -> None: ...


class JsonLinesAuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        if path != "-":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: AuditRecord) -> None:
        line = json.dumps({"type": "audit", **asdict(record)}, ensure_ascii=False)
        with self._lock:
            if self.path == "-":
                print(line, file=sys.stdout, flush=True)
            else:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")


@lru_cache
def _audit_log(path: str) -> JsonLinesAuditLog:
    return JsonLinesAuditLog(path)


def get_audit_log(settings: Annotated[Settings, Depends(get_settings)]) -> AuditLog:
    return _audit_log(settings.audit_log_path)
