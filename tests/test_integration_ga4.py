"""Integration tests for the ga4 dataset built from Google's public GA4 sample.

Build it once:  bq query --project_id=<project> --location=US --use_legacy_sql=false < scripts/build_ga4.sql
Run:            BQ_INTEGRATION_PROJECT=<project> python -m pytest tests/test_integration_ga4.py
"""
import os

import pytest

PROJECT = os.getenv("BQ_INTEGRATION_PROJECT")
pytestmark = pytest.mark.skipif(not PROJECT, reason="set BQ_INTEGRATION_PROJECT to run")


@pytest.fixture(scope="module")
def tools():
    from google.cloud import bigquery

    from app.bigquery_tools import BigQueryTools

    return BigQueryTools(
        client=bigquery.Client(project=PROJECT),
        project=PROJECT,
        allowed_datasets=["ga4"],
        pii_columns=[],
        max_bytes=100 * 1024**2,
        max_rows=100,
        timeout=60,
    )


def one_row(tools, sql):
    [row] = tools.execute_query(sql)["rows"]
    return row


def test_tables_and_descriptions(tools):
    tables = {t["table"] for t in tools.list_tables()}
    assert tables == {"ga4.sessions", "ga4.purchases", "ga4.purchase_items", "ga4.users"}
    schema = tools.get_schema("ga4", "sessions")
    # The descriptions are the semantic layer: metric definitions and caveats.
    assert "Conversion rate" in schema["description"]
    assert "ROAS" in schema["description"]
    columns = {c["name"]: c for c in schema["columns"]}
    assert "privacy placeholders" in columns["source"]["description"]


def test_revenue_consistent_across_tables(tools):
    row = one_row(
        tools,
        """
        SELECT
          (SELECT SUM(revenue_usd) FROM ga4.sessions) AS sessions_rev,
          (SELECT SUM(revenue_usd) FROM ga4.purchases) AS purchases_rev,
          (SELECT SUM(revenue_usd) FROM ga4.users) AS users_rev,
          (SELECT SUM(transactions) FROM ga4.sessions) AS session_tx,
          (SELECT COUNT(*) FROM ga4.purchases) AS purchases
        """,
    )
    assert row["sessions_rev"] > 0
    assert row["sessions_rev"] == pytest.approx(row["purchases_rev"])
    assert row["users_rev"] == pytest.approx(row["purchases_rev"])
    assert row["session_tx"] == row["purchases"]


def test_purchases_are_deduplicated(tools):
    row = one_row(tools, "SELECT COUNT(*) AS n, COUNT(DISTINCT purchase_id) AS ids FROM ga4.purchases")
    assert row["n"] == row["ids"]


def test_funnel_narrows(tools):
    row = one_row(
        tools,
        """
        SELECT COUNT(*) AS sessions, COUNTIF(viewed_item) AS viewed,
               COUNTIF(began_checkout) AS checkout, COUNTIF(purchased) AS purchased
        FROM ga4.sessions
        """,
    )
    assert row["sessions"] > row["viewed"] > row["checkout"] > row["purchased"] > 0


def test_one_row_per_session_and_user(tools):
    row = one_row(
        tools,
        """
        SELECT
          (SELECT COUNT(*) - COUNT(DISTINCT session_key) FROM ga4.sessions) AS dup_sessions,
          (SELECT COUNT(*) - COUNT(DISTINCT user_pseudo_id) FROM ga4.users) AS dup_users,
          (SELECT COUNT(DISTINCT user_pseudo_id) FROM ga4.sessions) AS session_users,
          (SELECT COUNT(*) FROM ga4.users) AS users
        """,
    )
    assert row["dup_sessions"] == 0
    assert row["dup_users"] == 0
    assert row["session_users"] == row["users"]


def test_typical_question_is_cheap(tools):
    result = tools.execute_query(
        "SELECT channel, COUNTIF(purchased) / COUNT(*) AS cr FROM ga4.sessions GROUP BY channel"
    )
    assert result["bytes_processed"] < 50 * 1024**2


def test_public_source_tables_stay_out_of_reach(tools):
    from app.bigquery_tools import QueryRejected

    with pytest.raises(QueryRejected, match="outside project"):
        tools.execute_query(
            "SELECT COUNT(*) FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_20210101`"
        )
