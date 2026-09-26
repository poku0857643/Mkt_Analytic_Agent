"""BigQuery operations exposed to the agent, with guardrails always applied.

The allowed datasets are fixed when the object is built (from the caller's role),
never taken from tool arguments, so the LLM cannot widen its own access.
"""
import datetime
import decimal
from typing import Any

from google.cloud import bigquery

from app.guardrails import check_cost, check_query


class QueryRejected(Exception):
    """A request was refused by a guardrail; the message says why."""


class DryRunEstimator:
    def __init__(self, client: bigquery.Client):
        self.client = client

    def estimate_bytes(self, sql: str) -> int:
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = self.client.query(sql, job_config=config)
        return job.total_bytes_processed or 0


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


class BigQueryTools:
    def __init__(
        self,
        client: bigquery.Client,
        project: str,
        allowed_datasets: list[str],
        pii_columns: list[str],
        max_bytes: int,
        max_rows: int,
        timeout: float,
    ):
        self.client = client
        self.project = project
        self.allowed_datasets = list(allowed_datasets)
        self.pii_columns = list(pii_columns)
        self.max_bytes = max_bytes
        self.max_rows = max_rows
        self.timeout = timeout
        self.estimator = DryRunEstimator(client)
        # Running total, so callers can charge scans even if the agent later fails.
        self.bytes_processed = 0

    def _require_dataset(self, dataset: str) -> None:
        if dataset not in self.allowed_datasets:
            raise QueryRejected(f"Dataset '{dataset}' is not allowed for your role")

    def list_tables(self, dataset: str | None = None) -> list[dict]:
        datasets = [dataset] if dataset else self.allowed_datasets
        tables = []
        for name in datasets:
            self._require_dataset(name)
            for item in self.client.list_tables(f"{self.project}.{name}"):
                tables.append(
                    {"table": f"{name}.{item.table_id}", "type": item.table_type}
                )
        return tables

    def get_schema(self, dataset: str, table: str) -> dict:
        self._require_dataset(dataset)
        if "." in table or "`" in table:
            # "x.other_dataset.t" would otherwise resolve outside the dataset.
            raise QueryRejected("Table name must not contain '.' or '`'")
        bq_table = self.client.get_table(f"{self.project}.{dataset}.{table}")
        pii = {
            entry.split(".")[2].lower()
            for entry in self.pii_columns
            if entry.lower().startswith(f"{dataset}.{table}.".lower())
        }
        return {
            "table": f"{dataset}.{table}",
            "description": bq_table.description or "",
            "num_rows": bq_table.num_rows,
            "columns": [
                {
                    "name": field.name,
                    "type": field.field_type,
                    "mode": field.mode,
                    "description": field.description or "",
                    # Tell the agent up front so it does not waste a retry.
                    "restricted_pii": field.name.lower() in pii,
                }
                for field in bq_table.schema
            ],
        }

    def execute_query(self, sql: str) -> dict:
        decision = check_query(sql, self.allowed_datasets, self.project, self.pii_columns)
        if not decision.approved:
            raise QueryRejected(decision.reason)

        cost = check_cost(sql, self.estimator, self.max_bytes)
        if not cost.approved:
            raise QueryRejected(cost.reason)

        config = bigquery.QueryJobConfig(maximum_bytes_billed=self.max_bytes)
        job = self.client.query(sql, job_config=config)
        # Fetch one extra row to know whether the result was cut off.
        rows = list(job.result(max_results=self.max_rows + 1, timeout=self.timeout))
        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]
        self.bytes_processed += job.total_bytes_processed or 0

        return {
            "tables": decision.tables,
            "bytes_processed": job.total_bytes_processed or 0,
            "row_count": len(rows),
            "truncated": truncated,
            "rows": [_json_safe(dict(row.items())) for row in rows],
        }
