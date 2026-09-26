"""Query guardrails: every SQL statement the agent wants to run passes through here.

The checks are deliberately conservative: when a query cannot be proven safe,
it is rejected with a reason the agent can use to revise it.
"""
from dataclasses import dataclass, field
from typing import Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

# Node types that must never appear anywhere in an approved query.
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
)


@dataclass
class Decision:
    approved: bool
    reason: str = ""
    tables: list[str] = field(default_factory=list)


def _reject(reason: str, tables: list[str] | None = None) -> Decision:
    return Decision(approved=False, reason=reason, tables=tables or [])


def check_query(
    sql: str,
    allowed_datasets: list[str],
    project: str,
    pii_columns: list[str] | None = None,
) -> Decision:
    """Approve only a single read-only SELECT over allowed datasets with no PII."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except (ParseError, TokenError) as e:
        return _reject(f"Could not parse SQL: {e}")

    if len(statements) != 1:
        return _reject(f"Expected exactly one statement, got {len(statements)}")
    ast = statements[0]

    if not isinstance(ast, exp.Query):
        return _reject(f"Only SELECT queries are allowed, got {ast.key.upper()}")
    forbidden = ast.find(*FORBIDDEN_NODES)
    if forbidden is not None:
        return _reject(f"Forbidden operation: {forbidden.key.upper()}")

    cte_names = {cte.alias_or_name for cte in ast.find_all(exp.CTE)}
    tables: list[exp.Table] = []
    for table in ast.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            # EXTERNAL_QUERY, ML.PREDICT, etc. can read outside the allowlist.
            return _reject(f"Table functions are not allowed: {table.sql('bigquery')}")
        if not table.db:
            if table.name in cte_names:
                continue
            return _reject(
                f"Table '{table.name}' must be qualified with its dataset (dataset.table)"
            )
        if table.catalog and table.catalog != project:
            return _reject(f"Queries outside project '{project}' are not allowed")
        if table.db not in allowed_datasets:
            return _reject(f"Dataset '{table.db}' is not allowed for your role")
        tables.append(table)

    table_names = sorted({f"{t.db}.{t.name}" for t in tables})

    pii_reason = _check_pii(ast, tables, pii_columns or [])
    if pii_reason:
        return _reject(pii_reason, table_names)

    return Decision(approved=True, tables=table_names)


def _check_pii(ast: exp.Expression, tables: list[exp.Table], pii_columns: list[str]) -> str:
    """Return a rejection reason if the query could read a PII column, else ''."""
    # "dataset.table" -> PII column names, all lowercased.
    pii_by_table: dict[str, set[str]] = {}
    for entry in pii_columns:
        dataset, table, column = entry.lower().split(".")
        pii_by_table.setdefault(f"{dataset}.{table}", set()).add(column)

    pii_tables = [t for t in tables if f"{t.db}.{t.name}".lower() in pii_by_table]
    if not pii_tables:
        return ""

    column_names = {c.name.lower() for c in ast.find_all(exp.Column)}
    for table in pii_tables:
        key = f"{table.db}.{table.name}".lower()
        hit = column_names & pii_by_table[key]
        if hit:
            return f"Column(s) {sorted(hit)} in {key} contain PII and cannot be queried"
        # Selecting a table (or its alias) as a value returns the whole row.
        if {table.name.lower(), table.alias_or_name.lower()} & column_names:
            return f"Selecting whole rows of {key} is not allowed: it contains PII"

    # COUNT(*) only counts rows; any other * can return column values.
    if any(not isinstance(star.parent, exp.Count) for star in ast.find_all(exp.Star)):
        names = ", ".join(sorted(f"{t.db}.{t.name}" for t in pii_tables))
        return f"SELECT * is not allowed on tables with PII ({names}); list the columns"
    return ""


class BytesEstimator(Protocol):
    def estimate_bytes(self, sql: str) -> int:
        """Bytes the query would scan (a BigQuery dry run in production)."""
        ...


def check_cost(sql: str, estimator: BytesEstimator, max_bytes: int) -> Decision:
    try:
        estimated = estimator.estimate_bytes(sql)
    except Exception as e:
        return _reject(f"Could not estimate query cost: {e}")
    if estimated > max_bytes:
        return _reject(
            f"Query would scan {estimated:,} bytes, over the {max_bytes:,} byte limit"
        )
    return Decision(approved=True)