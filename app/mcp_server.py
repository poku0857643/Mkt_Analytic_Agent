"""BigQuery MCP server.

In the API, build_server() is called per request with that user's datasets.
Standalone (for MCP Inspector / local testing):

    python -m app.mcp_server

uses GCP_PROJECT and MCP_ALLOWED_DATASETS from the environment / .env.
"""
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from app.bigquery_tools import BigQueryTools, QueryRejected

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


# Typed results give the agent an output schema for each tool.
class TableInfo(BaseModel):
    table: str
    type: str


class Column(BaseModel):
    name: str
    type: str
    mode: str
    description: str
    restricted_pii: bool


class TableSchema(BaseModel):
    table: str
    description: str
    num_rows: int | None
    columns: list[Column]


class QueryResult(BaseModel):
    tables: list[str]
    bytes_processed: int
    row_count: int
    truncated: bool
    rows: list[dict[str, Any]]


def build_server(tools: BigQueryTools) -> MCPServer:
    server = MCPServer(
        name="bigquery-analytics",
        instructions=(
            "Read-only access to marketing data in BigQuery. "
            f"Allowed datasets: {', '.join(tools.allowed_datasets)}. "
            "Always qualify tables as dataset.table. Columns marked restricted_pii "
            "cannot be queried, and SELECT * is refused on tables that have them."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    def list_tables(dataset: str | None = None) -> list[TableInfo]:
        """List tables in one allowed dataset, or in all of them if none is given."""
        try:
            return [TableInfo(**t) for t in tools.list_tables(dataset)]
        except QueryRejected as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ_ONLY)
    def get_schema(dataset: str, table: str) -> TableSchema:
        """Columns, types and descriptions of a table. Check restricted_pii before querying."""
        try:
            return TableSchema(**tools.get_schema(dataset, table))
        except QueryRejected as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ_ONLY)
    def execute_query(sql: str) -> QueryResult:
        """Run one read-only BigQuery SELECT. Rejected queries return the reason."""
        try:
            return QueryResult(**tools.execute_query(sql))
        except QueryRejected as e:
            raise ToolError(f"Query rejected: {e}") from e

    return server


def main() -> None:
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import bigquery

    from app.config import get_settings

    settings = get_settings()
    if not settings.mcp_allowed_datasets:
        raise SystemExit("Set MCP_ALLOWED_DATASETS, e.g. '[\"marketing\"]'")

    try:
        client = bigquery.Client(project=settings.gcp_project)
    except DefaultCredentialsError:
        raise SystemExit(
            "No GCP credentials. Run: gcloud auth application-default login"
        ) from None

    tools = BigQueryTools(
        client=client,
        project=settings.gcp_project,
        allowed_datasets=settings.mcp_allowed_datasets,
        pii_columns=settings.pii_columns,
        max_bytes=settings.max_bytes_billed,
        max_rows=settings.max_result_rows,
        timeout=settings.query_timeout_seconds,
    )
    build_server(tools).run()


if __name__ == "__main__":
    main()
