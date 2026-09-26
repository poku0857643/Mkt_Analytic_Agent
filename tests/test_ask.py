"""POST /ask end to end: auth -> agent (scripted fake Claude) -> fake BigQuery -> audit log."""
import json

import anthropic
import httpx
import pytest

from app.agent import AnalyticsAgent
from app.api import app, get_agent, get_audit_log, get_bigquery_client
from app.audit import JsonLinesAuditLog
from tests.conftest import ADMIN_KEY, ANALYST_KEY, NO_ACCESS_KEY
from tests.fake_bigquery import FakeClient
from tests.test_agent import ANSWER, GOOD_SQL, FakeAnthropic, final, query

QUESTION = {"question": "Which channel spent the most?"}


class MemoryAuditLog:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


class Harness:
    def __init__(self, client):
        self.client = client
        self.audit = MemoryAuditLog()
        self.bq = FakeClient()
        self.claude = None

    def script(self, *responses):
        self.claude = FakeAnthropic(responses)
        agent = AnalyticsAgent(self.claude, model="claude-opus-5")
        app.dependency_overrides[get_agent] = lambda: agent

    def ask(self, key=ANALYST_KEY, body=QUESTION):
        headers = {"X-API-Key": key} if key else {}
        return self.client.post("/ask", json=body, headers=headers)


@pytest.fixture
def h(client):
    harness = Harness(client)
    app.dependency_overrides[get_audit_log] = lambda: harness.audit
    app.dependency_overrides[get_bigquery_client] = lambda: harness.bq
    harness.script()  # no Claude calls unless a test scripts them
    return harness


def test_answer_and_audit_record(h):
    h.script(query(GOOD_SQL, "t1"), final())
    response = h.ask()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["summary"] == ANSWER["summary"]
    assert body["chart"]["points"] == [{"label": "search", "value": 1200.5}]
    assert body["sql_used"] == [GOOD_SQL]
    assert body["bytes_processed"] == 12_345

    [record] = h.audit.records
    assert record.request_id == body["request_id"]
    assert record.outcome == "answered"
    assert record.http_status == 200
    assert (record.user, record.role) == ("ana", "analyst")
    assert record.question == QUESTION["question"]
    assert [(q.sql, q.approved) for q in record.queries] == [(GOOD_SQL, True)]
    assert record.total_bytes_processed == 12_345
    assert record.turns == 2
    assert record.output_tokens == 40
    assert record.duration_ms >= 0


def test_datasets_come_from_role(h):
    h.script(final())
    h.ask(key=ANALYST_KEY)
    assert "Available datasets: marketing\n" in h.claude.requests[0]["messages"][0]["content"]

    h.script(final())
    h.ask(key=ADMIN_KEY)
    assert "Available datasets: marketing, customers" in h.claude.requests[0]["messages"][0]["content"]


def test_extra_fields_cannot_widen_access(h):
    h.script(final())
    h.ask(body={**QUESTION, "allowed_datasets": ["customers"]})
    assert "customers" not in h.claude.requests[0]["messages"][0]["content"]


def test_rejected_queries_are_audited(h):
    limitation = {"status": "limitation", "summary": "Not available.", "chart": None}
    h.script(query("DELETE FROM marketing.campaigns WHERE true", "t1"), final(limitation))
    body = h.ask().json()

    assert body["status"] == "limitation"
    assert body["sql_used"] == []
    [record] = h.audit.records
    assert record.outcome == "limitation"
    [q] = record.queries
    assert not q.approved
    assert "Query rejected" in q.detail
    assert h.bq.executed == []


def test_missing_key_is_denied_and_audited(h):
    response = h.ask(key=None)
    assert response.status_code == 401
    [record] = h.audit.records
    assert record.outcome == "denied"
    assert record.http_status == 401
    assert record.user is None
    assert record.key_hash_prefix is None
    assert record.question is None


def test_wrong_key_audits_hash_prefix_not_key(h):
    response = h.ask(key="stolen-or-typo-key")
    assert response.status_code == 401
    [record] = h.audit.records
    assert record.key_hash_prefix is not None and len(record.key_hash_prefix) == 8
    assert "stolen-or-typo-key" not in json.dumps(record.__dict__, default=str)


def test_role_without_datasets_is_403_and_audited(h):
    assert h.ask(key=NO_ACCESS_KEY).status_code == 403
    [record] = h.audit.records
    assert record.outcome == "denied"
    assert record.http_status == 403


def test_ai_service_error_is_502_and_audited(h):
    class Failing:
        async def create(self, **kwargs):
            raise anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            )

    h.script()
    h.claude.beta.messages = Failing()
    response = h.ask()

    assert response.status_code == 502
    assert "unavailable" in response.json()["detail"]
    [record] = h.audit.records
    assert record.outcome == "error"
    assert record.http_status == 502
    assert "APIConnectionError" in record.error


def test_unexpected_error_is_500_without_details(h):
    class Broken:
        async def create(self, **kwargs):
            raise RuntimeError("internal detail that must not leak")

    h.script()
    h.claude.beta.messages = Broken()
    response = h.ask()

    assert response.status_code == 500
    assert "internal detail" not in response.text
    [record] = h.audit.records
    assert record.http_status == 500
    assert "internal detail" in record.error


@pytest.mark.parametrize("question", ["", "hi", "x" * 2001])
def test_question_length_validated(h, question):
    assert h.ask(body={"question": question}).status_code == 422


def test_json_lines_audit_log(tmp_path):
    from app.audit import AuditQuery, AuditRecord

    path = tmp_path / "nested" / "audit.jsonl"
    log = JsonLinesAuditLog(str(path))
    log.write(AuditRecord(request_id="r1", outcome="denied", http_status=401))
    log.write(
        AuditRecord(
            request_id="r2",
            outcome="answered",
            http_status=200,
            user="ana",
            queries=[AuditQuery("SELECT 1", True, "", 10)],
        )
    )
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["request_id"] for r in lines] == ["r1", "r2"]
    assert lines[0]["type"] == "audit"
    assert lines[1]["queries"][0] == {
        "sql": "SELECT 1",
        "approved": True,
        "detail": "",
        "bytes_processed": 10,
    }
