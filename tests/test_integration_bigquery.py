"""Integration tests against real BigQuery sandbox data.

Seed the data once:  python scripts/seed_sandbox.py <project>
Run:                 BQ_INTEGRATION_PROJECT=<project> python -m pytest tests/test_integration_bigquery.py
"""
import asyncio
import os
from decimal import Decimal

import pytest

PROJECT = os.getenv("BQ_INTEGRATION_PROJECT")
pytestmark = pytest.mark.skipif(not PROJECT, reason="set BQ_INTEGRATION_PROJECT to run")

PII = ["customers.customers.email", "customers.customers.phone"]


@pytest.fixture(scope="module")
def client():
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT)


@pytest.fixture(scope="module")
def data():
    from scripts.seed_sandbox import generate_data

    return generate_data()


def make_tools(client, datasets=("marketing",), max_bytes=100 * 1024**2):
    from app.bigquery_tools import BigQueryTools

    return BigQueryTools(
        client=client,
        project=PROJECT,
        allowed_datasets=list(datasets),
        pii_columns=PII,
        max_bytes=max_bytes,
        max_rows=100,
        timeout=60,
    )


def test_list_tables(client):
    tables = {t["table"] for t in make_tools(client).list_tables()}
    assert tables == {"marketing.campaigns", "marketing.ad_spend_daily", "marketing.conversions"}


def test_get_schema_flags_pii(client):
    schema = make_tools(client, datasets=("customers",)).get_schema("customers", "customers")
    flagged = {c["name"] for c in schema["columns"] if c["restricted_pii"]}
    assert flagged == {"email", "phone"}
    assert schema["num_rows"] == 500


def test_aggregate_matches_generated_data(client, data):
    result = make_tools(client).execute_query(
        "SELECT c.channel, SUM(s.spend) AS spend "
        "FROM marketing.ad_spend_daily s JOIN marketing.campaigns c USING (campaign_id) "
        "GROUP BY c.channel ORDER BY c.channel"
    )
    channel_of = {c["campaign_id"]: c["channel"] for c in data["marketing.campaigns"]}
    expected: dict[str, Decimal] = {}
    for row in data["marketing.ad_spend_daily"]:
        ch = channel_of[row["campaign_id"]]
        expected[ch] = expected.get(ch, Decimal(0)) + row["spend"]

    got = {r["channel"]: r["spend"] for r in result["rows"]}
    assert got == pytest.approx({k: float(v) for k, v in expected.items()})
    assert result["bytes_processed"] >= 0
    assert result["truncated"] is False


def test_non_pii_columns_of_customers_are_queryable(client):
    result = make_tools(client, datasets=("customers",)).execute_query(
        "SELECT country, COUNT(*) AS n FROM customers.customers GROUP BY country"
    )
    assert sum(r["n"] for r in result["rows"]) == 500


def test_pii_query_rejected(client):
    from app.bigquery_tools import QueryRejected

    with pytest.raises(QueryRejected, match="PII"):
        make_tools(client, datasets=("customers",)).execute_query(
            "SELECT email FROM customers.customers LIMIT 5"
        )


def test_disallowed_dataset_rejected(client):
    from app.bigquery_tools import QueryRejected

    with pytest.raises(QueryRejected, match="not allowed"):
        make_tools(client).execute_query("SELECT country FROM customers.customers")


def test_real_dry_run_enforces_byte_limit(client):
    from app.bigquery_tools import QueryRejected

    with pytest.raises(QueryRejected, match="byte limit"):
        make_tools(client, max_bytes=1).execute_query(
            "SELECT campaign_id, spend FROM marketing.ad_spend_daily"
        )


def test_missing_table_rejected_by_dry_run(client):
    from app.bigquery_tools import QueryRejected

    with pytest.raises(QueryRejected, match="estimate"):
        make_tools(client).execute_query("SELECT x FROM marketing.no_such_table")


def test_mcp_server_end_to_end(client):
    from mcp import Client

    from app.mcp_server import build_server

    server = build_server(make_tools(client))

    async def run():
        async with Client(server) as c:
            ok = await c.call_tool(
                "execute_query", {"sql": "SELECT COUNT(*) AS n FROM marketing.campaigns"}
            )
            bad = await c.call_tool(
                "execute_query", {"sql": "DELETE FROM marketing.campaigns WHERE true"}
            )
            return ok, bad

    ok, bad = asyncio.run(run())
    assert not ok.is_error
    assert ok.structured_content["rows"] == [{"n": 20}]
    assert bad.is_error
    assert "Query rejected" in bad.content[0].text
