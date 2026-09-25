import pytest

from app.bigquery_tools import BigQueryTools, QueryRejected
from tests.fake_bigquery import FakeClient

PII = ["customers.customers.email"]


def make_tools(client=None, datasets=("marketing",), max_bytes=1_000_000, max_rows=3):
    return BigQueryTools(
        client=client or FakeClient(),
        project="test-project",
        allowed_datasets=list(datasets),
        pii_columns=PII,
        max_bytes=max_bytes,
        max_rows=max_rows,
        timeout=5.0,
    )


# list_tables


def test_list_tables_defaults_to_all_allowed_datasets():
    tools = make_tools(datasets=("marketing", "customers"))
    assert tools.list_tables() == [
        {"table": "marketing.campaigns", "type": "TABLE"},
        {"table": "marketing.conversions", "type": "TABLE"},
        {"table": "customers.customers", "type": "TABLE"},
    ]


def test_list_tables_one_dataset():
    tools = make_tools(datasets=("marketing", "customers"))
    assert [t["table"] for t in tools.list_tables("customers")] == ["customers.customers"]


def test_list_tables_disallowed_dataset():
    with pytest.raises(QueryRejected, match="not allowed"):
        make_tools().list_tables("customers")


# get_schema


def test_get_schema():
    schema = make_tools().get_schema("marketing", "campaigns")
    assert schema["table"] == "marketing.campaigns"
    assert schema["num_rows"] == 42
    assert schema["columns"][1] == {
        "name": "name",
        "type": "STRING",
        "mode": "NULLABLE",
        "description": "Campaign name",
        "restricted_pii": False,
    }


def test_get_schema_marks_pii_columns():
    schema = make_tools(datasets=("customers",)).get_schema("customers", "customers")
    flags = {c["name"]: c["restricted_pii"] for c in schema["columns"]}
    assert flags == {"id": False, "email": True, "signup_date": False}


def test_get_schema_disallowed_dataset():
    with pytest.raises(QueryRejected, match="not allowed"):
        make_tools().get_schema("customers", "customers")


@pytest.mark.parametrize("table", ["x.customers.customers", "t`; DROP", "a.b"])
def test_get_schema_rejects_dotted_table_names(table):
    with pytest.raises(QueryRejected, match="must not contain"):
        make_tools().get_schema("marketing", table)


# execute_query


def test_execute_query_runs_approved_sql():
    client = FakeClient()
    result = make_tools(client).execute_query("SELECT name, spend FROM marketing.campaigns")

    assert client.dry_runs == ["SELECT name, spend FROM marketing.campaigns"]
    sql, config = client.executed[0]
    assert config.maximum_bytes_billed == 1_000_000
    assert client.last_job.result_kwargs == {"max_results": 4, "timeout": 5.0}

    assert result["tables"] == ["marketing.campaigns"]
    assert result["bytes_processed"] == 12_345
    assert result["row_count"] == 3
    assert result["truncated"] is True
    # Decimal and date values become JSON-safe.
    assert result["rows"][0] == {"name": "Campaign 1", "spend": 100.5, "start_date": "2026-07-01"}


def test_execute_query_not_truncated_when_rows_fit():
    result = make_tools(max_rows=10).execute_query("SELECT name FROM marketing.campaigns")
    assert result["row_count"] == 5
    assert result["truncated"] is False


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE marketing.campaigns",
        "SELECT * FROM customers.customers",
        "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')",
        "SELECT 1; DELETE FROM marketing.campaigns WHERE true",
    ],
)
def test_rejected_sql_never_reaches_bigquery(sql):
    client = FakeClient()
    with pytest.raises(QueryRejected):
        make_tools(client).execute_query(sql)
    assert client.dry_runs == []
    assert client.executed == []


def test_pii_column_rejected_before_bigquery():
    client = FakeClient()
    tools = make_tools(client, datasets=("customers",))
    with pytest.raises(QueryRejected, match="PII"):
        tools.execute_query("SELECT email FROM customers.customers")
    assert client.executed == []


def test_expensive_query_is_dry_run_but_not_executed():
    client = FakeClient(dry_run_bytes=5_000_000)
    with pytest.raises(QueryRejected, match="byte limit"):
        make_tools(client).execute_query("SELECT name FROM marketing.campaigns")
    assert len(client.dry_runs) == 1
    assert client.executed == []


def test_dry_run_failure_is_rejected():
    client = FakeClient(dry_run_bytes=RuntimeError("Not found: Table marketing.nope"))
    with pytest.raises(QueryRejected, match="Not found"):
        make_tools(client).execute_query("SELECT a FROM marketing.nope")
    assert client.executed == []
