"""
Semantic resolver for transaction category-like columns.

This module maps user language such as "2BHK", "mrkt", or "residential"
to the exact values already present in the database. It is intentionally
import-safe: no database or LLM calls run at import time.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import psycopg2
from openai import OpenAI


DEFAULT_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://suraj:suraj@localhost:5432/postgres",
)

TRANSACTION_TABLE = "transactions"
PROJECT_TABLE = "projects"

SEMANTIC_CATEGORY_COLUMNS = [
    "transaction_category",
    "property_type",
    "unit_configuration",
    "project_type",
    "sale_type",
    "furnishing_status",
    "condition_status",
    "facing_direction",
    "view_type",
    "bank_type",
]

PROJECT_SEMANTIC_CATEGORY_COLUMNS = [
    "project_type",
]

TABLE_SEMANTIC_CATEGORY_COLUMNS = {
    TRANSACTION_TABLE: SEMANTIC_CATEGORY_COLUMNS,
    PROJECT_TABLE: PROJECT_SEMANTIC_CATEGORY_COLUMNS,
}

_ALLOWED_TABLES = set(TABLE_SEMANTIC_CATEGORY_COLUMNS)
_ALLOWED_COLUMNS = {
    column
    for columns in TABLE_SEMANTIC_CATEGORY_COLUMNS.values()
    for column in columns
}


def _parse_json(text: str, default: Any) -> Any:
    text = (text or "").strip()
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return default


def _ensure_safe_table(table_name: str) -> str:
    if table_name not in _ALLOWED_TABLES:
        raise ValueError(f"Unsupported semantic table: {table_name}")
    return table_name


def _ensure_safe_columns(columns: list[str], table_name: str | None = None) -> list[str]:
    allowed_columns = (
        set(TABLE_SEMANTIC_CATEGORY_COLUMNS[table_name])
        if table_name is not None
        else _ALLOWED_COLUMNS
    )
    safe = []
    for col in columns:
        if col not in allowed_columns:
            raise ValueError(f"Unsupported semantic column: {col}")
        if col not in safe:
            safe.append(col)
    return safe


def _clean_values(values: list[Any]) -> list[str]:
    cleaned = []
    seen = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if not text or text.lower() in seen:
            continue
        cleaned.append(text)
        seen.add(text.lower())
    return cleaned


def get_distinct_values(
    table_name: str,
    columns: list[str],
    db_executor: Callable[[str], list[dict]] | None = None,
    db_url: str | None = None,
    limit: int = 300,
) -> dict[str, list[str]]:
    """
    Fetch distinct DB values for whitelisted semantic columns.

    Prefer db_executor when running inside the agent pipeline, because it uses
    the app's existing database execution path. psycopg2 is kept for standalone
    usage and quick local checks.
    """
    table_name = _ensure_safe_table(table_name)
    columns = _ensure_safe_columns(columns, table_name=table_name)
    result: dict[str, list[str]] = {}

    if db_executor is not None:
        for col in columns:
            sql = (
                f'SELECT DISTINCT "{col}" AS value '
                f'FROM "{table_name}" '
                f'WHERE "{col}" IS NOT NULL AND "{col}"::text <> \'\' '
                f'ORDER BY "{col}" '
                f"LIMIT {int(limit)}"
            )
            rows = db_executor(sql) or []
            result[col] = _clean_values([row.get("value") for row in rows])
        return result

    conn = psycopg2.connect(db_url or DEFAULT_DB_URL)
    try:
        cur = conn.cursor()
        for col in columns:
            cur.execute(
                f'SELECT DISTINCT "{col}" '
                f'FROM "{table_name}" '
                f'WHERE "{col}" IS NOT NULL AND "{col}"::text <> \'\' '
                f'ORDER BY "{col}" '
                f"LIMIT %s",
                (limit,),
            )
            result[col] = _clean_values([row[0] for row in cur.fetchall()])
        cur.close()
    finally:
        conn.close()

    return result


def resolve_query(
    query_value: str,
    distinct_values: dict[str, list[str]],
    client: OpenAI | None = None,
    model: str = "gpt-5.1",
) -> dict[str, list[str]]:
    """
    Resolve one user phrase against distinct DB values for one or more columns.
    """
    query_value = str(query_value or "").strip()
    if not query_value:
        return {col: [] for col in distinct_values}

    openai_client = client or OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    prompt = f"""You are a semantic search resolver for real-estate database filters.

User query: "{query_value}"

For each column below, pick the best matching exact values from the list.
Return ONLY a JSON object like: {{"column_name": ["exact_db_value"]}}

Column values:
{json.dumps(distinct_values, indent=2, ensure_ascii=False)}

Rules:
- Return exact values copied from the lists, never invented values.
- Match by meaning, synonyms, abbreviations, spelling variants, and common usage.
- Examples: "apartment" can match "flat"; "market" can match "shop"; "2BHK" can match "2 BHK" or "2B/R".
- If no match, return an empty list for that column.
- No explanation, only JSON."""

    response = openai_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content or ""
    parsed = _parse_json(content, default={})

    resolved: dict[str, list[str]] = {}
    for col, values in distinct_values.items():
        allowed = {str(value) for value in values}
        matches = parsed.get(col) if isinstance(parsed, dict) else []
        if not isinstance(matches, list):
            matches = []
        resolved[col] = [str(value) for value in matches if str(value) in allowed]
    return resolved


def _append_value(
    bucket: dict[str, list[str]],
    column: str,
    value: Any,
    allowed_columns: set[str] | None = None,
) -> None:
    allowed_columns = allowed_columns or _ALLOWED_COLUMNS
    if column not in allowed_columns:
        return
    if value in (None, "", [], {}):
        return
    if isinstance(value, dict):
        value = value.get("value") or value.get("name")
    text = str(value).strip()
    if not text:
        return
    bucket.setdefault(column, [])
    if text.lower() not in {item.lower() for item in bucket[column]}:
        bucket[column].append(text)


def collect_intent_category_queries(
    intent: dict,
    columns: list[str] | None = None,
) -> dict[str, list[str]]:
    """
    Collect category-like raw values from the intent.

    These raw values may be user wording; the resolver turns them into exact DB
    values before SQL generation.
    """
    queries: dict[str, list[str]] = {}
    semantic_columns = columns or SEMANTIC_CATEGORY_COLUMNS
    allowed_columns = set(semantic_columns)
    entities = intent.get("entities") or {}
    filters = intent.get("filters") or {}

    category_filters = entities.get("category_filters") or {}
    if isinstance(category_filters, dict):
        for column, value in category_filters.items():
            if isinstance(value, list):
                for item in value:
                    _append_value(queries, column, item, allowed_columns)
            else:
                _append_value(queries, column, value, allowed_columns)

    if "property_type" in allowed_columns:
        for item in entities.get("property_types") or []:
            _append_value(queries, "property_type", item, allowed_columns)

    for column in semantic_columns:
        _append_value(queries, column, filters.get(column), allowed_columns)

    extra = filters.get("extra") or []
    if isinstance(extra, list):
        for item in extra:
            if not isinstance(item, dict):
                continue
            column = item.get("field") or item.get("column")
            value = item.get("value")
            if isinstance(value, list):
                for raw in value:
                    _append_value(queries, str(column), raw, allowed_columns)
            else:
                _append_value(queries, str(column), value, allowed_columns)

    return queries


def resolve_intent_category_filters(
    intent: dict,
    client: OpenAI,
    db_executor: Callable[[str], list[dict]],
    table_name: str = TRANSACTION_TABLE,
    columns: list[str] | None = None,
    model: str = "gpt-5.1",
) -> dict[str, list[str]]:
    """
    Resolve category-like intent values and write exact DB values onto intent.

    The enriched shape is:
      intent["semantic_resolved_filters"] = {"unit_configuration": ["2 BHK"]}
    """
    table_name = _ensure_safe_table(table_name)
    semantic_columns = columns or TABLE_SEMANTIC_CATEGORY_COLUMNS[table_name]
    raw_queries = collect_intent_category_queries(intent, columns=semantic_columns)
    if not raw_queries:
        intent.setdefault("semantic_resolved_filters", {})
        return {}

    distinct_values = get_distinct_values(
        table_name=table_name,
        columns=list(raw_queries.keys()),
        db_executor=db_executor,
    )

    resolved: dict[str, list[str]] = {}
    for column, raw_values in raw_queries.items():
        column_values = {column: distinct_values.get(column, [])}
        for raw_value in raw_values:
            matches = resolve_query(
                query_value=raw_value,
                distinct_values=column_values,
                client=client,
                model=model,
            ).get(column, [])
            for match in matches:
                resolved.setdefault(column, [])
                if match not in resolved[column]:
                    resolved[column].append(match)

    intent["semantic_category_queries"] = raw_queries
    intent["semantic_resolved_filters"] = {
        column: values for column, values in resolved.items() if values
    }
    return intent["semantic_resolved_filters"]


def search_multiple_columns(
    table_name: str,
    columns: list[str],
    query_value: str,
    top_k: int = 10,
    client: OpenAI | None = None,
    db_url: str | None = None,
) -> list[dict]:
    """
    Standalone helper retained for local debugging.
    """
    table_name = _ensure_safe_table(table_name)
    columns = _ensure_safe_columns(columns)
    distinct_values = get_distinct_values(table_name, columns, db_url=db_url)
    resolved = resolve_query(query_value, distinct_values, client=client)

    conditions, params = [], []
    for col, values in resolved.items():
        if values:
            placeholders = ", ".join(["%s"] * len(values))
            conditions.append(f'"{col}" IN ({placeholders})')
            params.extend(values)

    if not conditions:
        return []

    query = f'SELECT * FROM "{table_name}" WHERE {" OR ".join(conditions)} LIMIT %s'
    params.append(top_k)

    conn = psycopg2.connect(db_url or DEFAULT_DB_URL)
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.close()
        return rows
    finally:
        conn.close()


if __name__ == "__main__":
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    rows = search_multiple_columns(
        table_name="transactions",
        columns=["property_type"],
        query_value="mrkt",
        top_k=3,
        client=openai_client,
    )
    print(json.dumps(rows, indent=2, default=str))
