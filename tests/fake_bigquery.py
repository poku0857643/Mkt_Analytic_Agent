"""In-memory stand-in for google.cloud.bigquery.Client used by unit tests."""
import datetime
import decimal
from types import SimpleNamespace

from google.cloud import bigquery

TABLES = {
    "marketing": ["campaigns", "conversions"],
    "customers": ["customers"],
}

SCHEMAS = {
    "marketing.campaigns": [
        bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
        bigquery.SchemaField("name", "STRING", description="Campaign name"),
        bigquery.SchemaField("spend", "NUMERIC"),
        bigquery.SchemaField("start_date", "DATE"),
    ],
    "customers.customers": [
        bigquery.SchemaField("id", "INT64"),
        bigquery.SchemaField("email", "STRING"),
        bigquery.SchemaField("signup_date", "DATE"),
    ],
}

ROWS = [
    {
        "name": f"Campaign {i}",
        "spend": decimal.Decimal("100.50") * i,
        "start_date": datetime.date(2026, 7, i),
    }
    for i in range(1, 6)
]


class FakeJob:
    def __init__(self, bytes_processed, rows):
        self.total_bytes_processed = bytes_processed
        self._rows = rows
        self.result_kwargs = None

    def result(self, max_results=None, timeout=None):
        self.result_kwargs = {"max_results": max_results, "timeout": timeout}
        return iter(self._rows[:max_results])


class FakeClient:
    def __init__(self, dry_run_bytes=1_000, rows=ROWS):
        self.dry_run_bytes = dry_run_bytes
        self.rows = rows
        self.dry_runs: list[str] = []
        self.executed: list[tuple[str, bigquery.QueryJobConfig]] = []
        self.last_job: FakeJob | None = None

    def list_tables(self, dataset_ref):
        dataset = dataset_ref.split(".")[-1]
        return [
            SimpleNamespace(table_id=t, table_type="TABLE") for t in TABLES.get(dataset, [])
        ]

    def get_table(self, table_ref):
        _, dataset, table = table_ref.split(".")
        return SimpleNamespace(
            description=f"{table} table",
            num_rows=42,
            schema=SCHEMAS[f"{dataset}.{table}"],
        )

    def query(self, sql, job_config=None):
        if job_config is not None and job_config.dry_run:
            if isinstance(self.dry_run_bytes, Exception):
                raise self.dry_run_bytes
            self.dry_runs.append(sql)
            return FakeJob(self.dry_run_bytes, [])
        self.executed.append((sql, job_config))
        self.last_job = FakeJob(12_345, self.rows)
        return self.last_job
