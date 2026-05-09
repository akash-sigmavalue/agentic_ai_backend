import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from langgraph.prebuilt import create_react_agent
from sqlalchemy import text

from agents.shared.parsers import (
    collect_agent_trace,
    extract_text_from_content,
    try_parse_payload,
)
from agents.shared.schema_helpers import normalize_resolver_shape
from agents.shared.usage import normalize_usage_metadata
from agents.uploaded_data_grounding_agent.prompts import get_uploaded_data_grounding_prompt
from agents.uploaded_data_grounding_agent.tools import (
    UPLOADED_DATA_TOOLS,
    validate_user_file_table_name,
)
from database.db import SessionLocal, engine


CREATE_UPLOADS_REGISTRY_SQL = """
CREATE SCHEMA IF NOT EXISTS user_data;

CREATE TABLE IF NOT EXISTS user_data.uploads_registry (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    table_name TEXT NOT NULL UNIQUE,
    columns_info JSONB NOT NULL,
    row_count INTEGER NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

def _sanitize_identifier(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}"
    return cleaned


def _clean_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    seen: dict[str, int] = {}
    cleaned_columns = []

    for index, column in enumerate(df.columns):
        base_name = _sanitize_identifier(str(column), f"column_{index + 1}")
        count = seen.get(base_name, 0)
        seen[base_name] = count + 1
        cleaned_columns.append(base_name if count == 0 else f"{base_name}_{count + 1}")

    df = df.copy()
    df.columns = cleaned_columns
    return df


def _read_uploaded_dataframe(file_path: str, filename: str) -> pd.DataFrame:
    suffix = Path(filename or file_path).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(file_path)
    if suffix in {".xls", ".xlsx"}:
        return pd.read_excel(file_path)
    raise ValueError("Only CSV, XLS, and XLSX files are supported")


def _ensure_user_data_registry() -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_UPLOADS_REGISTRY_SQL))


def validate_uploaded_table_for_user(user_id: int, full_table_name: str) -> None:
    validate_user_file_table_name(full_table_name)
    _ensure_user_data_registry()

    db = SessionLocal()
    try:
        row = db.execute(
            text(
                """
                SELECT 1
                FROM user_data.uploads_registry
                WHERE user_id = :user_id AND table_name = :table_name
                LIMIT 1
                """
            ),
            {
                "user_id": int(user_id),
                "table_name": full_table_name,
            },
        ).first()
        if row is None:
            raise ValueError("Uploaded file table was not found for this user")
    finally:
        db.close()


def ingest_uploaded_file(user_id: int, file_path: str, filename: str) -> str:
    _ensure_user_data_registry()

    df = _read_uploaded_dataframe(file_path, filename)
    if df.empty:
        raise ValueError("Uploaded file contains no rows")

    df = _clean_dataframe_columns(df)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    sanitized_name = _sanitize_identifier(Path(filename).stem, "upload")
    prefix = f"user_{int(user_id)}_{timestamp}_"
    table_part = f"{prefix}{sanitized_name}"[:63].rstrip("_")
    full_table_name = f"user_data.{table_part}"

    columns_info = [
        {
            "name": column,
            "dtype": str(dtype),
        }
        for column, dtype in df.dtypes.items()
    ]

    with engine.begin() as conn:
        df.to_sql(
            name=table_part,
            con=conn,
            schema="user_data",
            if_exists="fail",
            index=False,
            method="multi",
        )
        conn.execute(
            text(
                """
                INSERT INTO user_data.uploads_registry
                    (user_id, original_filename, table_name, columns_info, row_count)
                VALUES
                    (:user_id, :original_filename, :table_name, CAST(:columns_info AS JSONB), :row_count)
                """
            ),
            {
                "user_id": int(user_id),
                "original_filename": filename,
                "table_name": full_table_name,
                "columns_info": json.dumps(columns_info),
                "row_count": int(len(df)),
            },
        )

    return full_table_name


def create_file_data_react_agent(llm):
    return create_react_agent(
        llm,
        tools=UPLOADED_DATA_TOOLS,
        prompt=get_uploaded_data_grounding_prompt(),
    )


def _extract_file_resolver_output(agent_result: dict) -> dict:
    for msg in reversed(agent_result.get("messages", [])):
        content = extract_text_from_content(getattr(msg, "content", ""))
        parsed = try_parse_payload(content)
        if isinstance(parsed, dict) and "can_answer" in parsed:
            return parsed

        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if match:
            parsed = try_parse_payload(match.group(0))
            if isinstance(parsed, dict) and "can_answer" in parsed:
                return parsed

    agent_trace = collect_agent_trace(agent_result.get("messages", []))
    raise RuntimeError(
        "File data agent did not return DBResolverPlanOut JSON. "
        f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
    )


def run_uploaded_data_grounding_agent(
    llm,
    user_query: str,
    semantic_schema_dict: dict,
    semantic_schema_json: str,
    uploaded_table_name: str,
) -> tuple[dict, dict[str, object] | None]:
    react_agent = create_file_data_react_agent(llm)
    user_message = f"""
Original User Query:
{user_query}

Uploaded Table Name:
{uploaded_table_name}

Semantic UI Schema from Agent 1:
{semantic_schema_json}

Resolve this semantic schema against the uploaded file table and return DBResolverPlanOut JSON.
"""
    result = react_agent.invoke({"messages": [("user", user_message)]})
    resolver_dict = normalize_resolver_shape(_extract_file_resolver_output(result))
    resolver_dict.setdefault("can_answer", True)
    resolver_dict.setdefault("reason", None)
    resolver_dict.setdefault("grounded_schema", semantic_schema_dict)
    resolver_dict.setdefault("semantic_mapping", {})
    resolver_dict.setdefault("sql_queries", [])

    usage = None
    for msg in reversed(result.get("messages", [])):
        usage = normalize_usage_metadata(msg)
        if usage:
            break

    print("\n" + "=" * 80)
    print("AGENT 2F OUTPUT - FILE GROUNDED SCHEMA + SQL")
    print("=" * 80)
    print(json.dumps(resolver_dict, indent=2, default=str))
    print("=" * 80 + "\n")

    return resolver_dict, usage
