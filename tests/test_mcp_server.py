"""Drive the MCP server through a real in-process MCP client."""
import asyncio

from mcp import Client

from app.bigquery_tools import BigQueryTools
from app.mcp_server import build_server
from tests.fake_bigquery import FakeClient


def make_server(client):
    tools = BigQueryTools(
        client=client,
        project="test-project",
        allowed_datasets=["marketing"],
        pii_columns=["customers.customers.email"],
        max_bytes=1_000_000,
        max_rows=10,
        timeout=5.0,
    )
    return build_server(tools)


def call(server, tool, args=None):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, args or {})

    return asyncio.run(run())


def test_lists_three_read_only_tools():
    async def run():
        async with Client(make_server(FakeClient())) as client:
            return await client.list_tools()

    tools = {t.name: t for t in asyncio.run(run()).tools}
    assert set(tools) == {"list_tables", "get_schema", "execute_query"}
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert all(t.output_schema for t in tools.values())


def test_list_tables_over_mcp():
    result = call(make_server(FakeClient()), "list_tables")
    assert not result.is_error
    assert [t["table"] for t in result.structured_content["result"]] == [
        "marketing.campaigns",
        "marketing.conversions",
    ]


def test_execute_query_over_mcp():
    result = call(
        make_server(FakeClient()),
        "execute_query",
        {"sql": "SELECT name FROM marketing.campaigns"},
    )
    assert not result.is_error
    assert result.structured_content["row_count"] == 5


def test_rejected_query_is_a_tool_error_with_reason():
    client = FakeClient()
    result = call(
        make_server(client),
        "execute_query",
        {"sql": "DROP TABLE marketing.campaigns"},
    )
    assert result.is_error
    assert "Query rejected" in result.content[0].text
    assert client.executed == []


def test_disallowed_dataset_is_a_tool_error():
    result = call(make_server(FakeClient()), "list_tables", {"dataset": "customers"})
    assert result.is_error
    assert "not allowed" in result.content[0].text


def test_datasets_cannot_be_widened_through_arguments():
    # The allowlist is fixed at build time; extra arguments are not accepted.
    client = FakeClient()
    result = call(
        make_server(client),
        "execute_query",
        {"sql": "SELECT id FROM customers.customers", "allowed_datasets": ["customers"]},
    )
    assert result.is_error
    assert client.executed == []
