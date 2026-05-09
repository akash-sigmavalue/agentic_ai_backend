import json
import re

from langchain_core.tools import tool
from sqlalchemy import inspect, text

from database.db import SessionLocal, engine


USER_FILE_TABLE_RE = re.compile(r"^user_data\.user_\d+_\d{14,20}_[a-z0-9_]+$")


def validate_user_file_table_name(full_table_name: str) -> tuple[str, str]:
    normalized = (full_table_name or "").strip()
    if not USER_FILE_TABLE_RE.fullmatch(normalized):
        raise ValueError("Invalid uploaded file table name")
    schema_name, table_name = normalized.split(".", 1)
    return schema_name, table_name


def quote_full_table_name(full_table_name: str) -> str:
    schema_name, table_name = validate_user_file_table_name(full_table_name)
    return f'"{schema_name}"."{table_name}"'


@tool
def get_table_columns(full_table_name: str) -> str:
    """Return uploaded table columns and PostgreSQL types as JSON."""
    schema_name, table_name = validate_user_file_table_name(full_table_name)
    inspector = inspect(engine)
    columns = inspector.get_columns(table_name, schema=schema_name)
    return json.dumps(
        [
            {
                "name": column["name"],
                "type": str(column["type"]),
            }
            for column in columns
        ],
        default=str,
    )


@tool
def preview_table_rows(full_table_name: str, limit: int = 5) -> str:
    """Return the first rows from an uploaded table as JSON."""
    quoted_table = quote_full_table_name(full_table_name)
    safe_limit = max(1, min(int(limit or 5), 50))
    db = SessionLocal()
    try:
        result = db.execute(text(f"SELECT * FROM {quoted_table} LIMIT :limit"), {"limit": safe_limit})
        return json.dumps([dict(row) for row in result.mappings().all()], default=str)
    finally:
        db.close()


@tool
def execute_sql_on_file_table(full_table_name: str, sql_query: str) -> str:
    """Run a read-only PostgreSQL SELECT query against the uploaded table and return JSON rows."""
    quoted_table = quote_full_table_name(full_table_name)
    normalized_sql = (sql_query or "").strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", normalized_sql):
        raise ValueError("Only SELECT/CTE queries are allowed")
    if ";" in normalized_sql:
        raise ValueError("Multiple SQL statements are not allowed")
    if full_table_name not in normalized_sql and quoted_table not in normalized_sql:
        raise ValueError("SQL must reference the uploaded table")

    db = SessionLocal()
    try:
        result = db.execute(text(normalized_sql))
        return json.dumps([dict(row) for row in result.mappings().all()], default=str)
    finally:
        db.close()


UPLOADED_DATA_TOOLS = [
    get_table_columns,
    preview_table_rows,
    execute_sql_on_file_table,
]
