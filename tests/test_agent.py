"""Agent loop tests with a scripted fake Claude and the real MCP server over fake BigQuery."""
import asyncio
import json
from types import SimpleNamespace

from app.agent import FALLBACK_BETA, AnalyticsAgent
from app.bigquery_tools import BigQueryTools
from app.mcp_server import build_server
from tests.fake_bigquery import FakeClient

GOOD_SQL = "SELECT channel, SUM(spend) AS spend FROM marketing.campaigns GROUP BY channel"

ANSWER = {
    "status": "answered",
    "summary": "Search spent the most.",
    "chart": {
        "type": "bar",
        "title": "Spend by channel",
        "x_label": "Channel",
        "y_label": "USD",
        "points": [{"label": "search", "value": 1200.5}],
    },
}


def tool_use(name, args, id_):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def reply(stop_reason, *blocks):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


def final(answer=ANSWER):
    return reply("end_turn", SimpleNamespace(type="text", text=json.dumps(answer)))


def query(sql, id_):
    return reply("tool_use", tool_use("execute_query", {"sql": sql}, id_))


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def create(self, **kwargs):
        # Snapshot messages: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.script.pop(0)


class FakeAnthropic:
    def __init__(self, script):
        self.beta = SimpleNamespace(messages=FakeMessages(script))

    @property
    def requests(self):
        return self.beta.messages.requests


def run(script, bq=None, max_query_retries=3, max_turns=12):
    claude = FakeAnthropic(script)
    bq = bq or FakeClient()
    tools = BigQueryTools(
        client=bq,
        project="test-project",
        allowed_datasets=["marketing"],
        pii_columns=["customers.customers.email"],
        max_bytes=1_000_000,
        max_rows=10,
        timeout=5.0,
    )
    agent = AnalyticsAgent(
        claude, model="claude-opus-5", max_query_retries=max_query_retries, max_turns=max_turns
    )
    result = asyncio.run(agent.ask("Which channel spent most?", build_server(tools), ["marketing"]))
    return result, claude, bq


def tool_results(request):
    """The tool_result blocks in the last user message of a request."""
    return request["messages"][-1]["content"]


def test_happy_path_returns_structured_answer_and_sql():
    result, claude, bq = run(
        [
            reply("tool_use", tool_use("get_schema", {"dataset": "marketing", "table": "campaigns"}, "t1")),
            query(GOOD_SQL, "t2"),
            final(),
        ]
    )
    assert result.answer.status == "answered"
    assert result.answer.chart.points[0].value == 1200.5
    assert result.sql_used == [GOOD_SQL]
    assert result.queries[0].bytes_processed == 12_345
    assert result.turns == 3
    assert result.input_tokens == 300
    assert [sql for sql, _ in bq.executed] == [GOOD_SQL]

    # The schema result reached Claude as a tool_result.
    schema_result = tool_results(claude.requests[1])[0]
    assert schema_result["tool_use_id"] == "t1"
    assert not schema_result["is_error"]
    assert "campaigns" in schema_result["content"]


def test_request_shape():
    _, claude, _ = run([final()])
    req = claude.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["fallbacks"] == "default"
    assert req["betas"] == [FALLBACK_BETA]
    assert req["thinking"] == {"type": "adaptive"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"]["effort"] == "high"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert [t["name"] for t in req["tools"]] == ["execute_query", "get_schema", "list_tables"]
    assert "Available datasets: marketing" in req["messages"][0]["content"]


def test_tools_and_system_prompt_identical_across_turns():
    # Required for prompt caching: only messages may change between turns.
    _, claude, _ = run([query(GOOD_SQL, "t1"), final()])
    first, second = claude.requests
    assert first["tools"] == second["tools"]
    assert first["system"] == second["system"]


def test_rejected_query_is_reported_and_agent_can_fix_it():
    result, claude, bq = run([query("DROP TABLE marketing.campaigns", "t1"), query(GOOD_SQL, "t2"), final()])

    rejected = tool_results(claude.requests[1])[0]
    assert rejected["is_error"]
    assert "Query rejected" in rejected["content"]
    assert "2 query attempt(s) left" in rejected["content"]

    assert [q.approved for q in result.queries] == [False, True]
    assert result.sql_used == [GOOD_SQL]
    assert [sql for sql, _ in bq.executed] == [GOOD_SQL]


def test_retry_limit_stops_further_queries():
    bad = "SELECT * FROM finance.revenue"
    limitation = {"status": "limitation", "summary": "Finance data is not available.", "chart": None}
    result, claude, bq = run(
        [
            query(bad, "t1"),
            query(bad, "t2"),
            query(bad, "t3"),
            query(GOOD_SQL, "t4"),  # over the limit: must not run
            final(limitation),
        ]
    )
    assert "No query attempts left" in tool_results(claude.requests[3])[0]["content"]

    blocked = tool_results(claude.requests[4])[0]
    assert blocked["is_error"]
    assert "retry limit reached" in blocked["content"].lower()

    assert bq.dry_runs == []
    assert bq.executed == []
    assert [q.detail for q in result.queries][-1] == "retry limit reached; not run"
    assert result.answer.status == "limitation"


def test_parallel_tool_calls_return_results_in_one_message():
    result, claude, _ = run(
        [
            reply(
                "tool_use",
                tool_use("list_tables", {}, "a"),
                tool_use("get_schema", {"dataset": "marketing", "table": "campaigns"}, "b"),
            ),
            final(),
        ]
    )
    results = tool_results(claude.requests[1])
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_disallowed_dataset_in_tool_is_error_but_not_a_query_rejection():
    result, claude, _ = run(
        [reply("tool_use", tool_use("list_tables", {"dataset": "customers"}, "t1")), final()]
    )
    assert tool_results(claude.requests[1])[0]["is_error"]
    assert result.queries == []


def test_refusal_returns_limitation():
    result, _, _ = run([reply("refusal")])
    assert result.answer.status == "limitation"
    assert "declined" in result.answer.summary


def test_max_tokens_returns_limitation():
    result, _, _ = run([reply("max_tokens", SimpleNamespace(type="text", text='{"status": "ans'))])
    assert result.answer.status == "limitation"
    assert "max_tokens" in result.answer.summary


def test_turn_cap():
    result, _, _ = run([query(GOOD_SQL, f"t{i}") for i in range(3)], max_turns=3)
    assert result.answer.status == "limitation"
    assert "3 steps" in result.answer.summary
    assert result.turns == 3


def test_malformed_final_answer_is_a_limitation():
    result, _, _ = run([reply("end_turn", SimpleNamespace(type="text", text="not json"))])
    assert result.answer.status == "limitation"
    assert "unexpected format" in result.answer.summary
