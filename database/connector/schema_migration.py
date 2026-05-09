from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


_ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "oauth_connections": [
        ("gmail_connector_id", "TEXT"),
        ("gmail_history_id", "TEXT"),
        ("gmail_watch_expiration", "TIMESTAMP WITH TIME ZONE"),
    ],
    "automation_rules": [
        ("connector_type", "TEXT"),
        ("sender_name", "TEXT"),
        ("sender_email", "TEXT"),
        ("subject_filter", "TEXT"),
        ("keyword_filter", "JSON"),
        ("operation", "TEXT"),
        ("tone", "TEXT"),
        ("output_requirement", "JSON"),
        ("last_processed_message_id", "TEXT"),
        ("updated_at", "TIMESTAMP WITH TIME ZONE"),
    ],
}


def ensure_additive_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        return

    with engine.begin() as connection:
        for table_name, columns in _ADDITIVE_COLUMNS.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_ddl in columns:
                if column_name in existing_columns:
                    continue
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_ddl}"))
