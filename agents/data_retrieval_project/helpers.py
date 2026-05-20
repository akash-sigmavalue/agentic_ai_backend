"""
Project Query Builder Helpers
=============================
Utility functions for space filter extraction, JSON parsing, and SQL validation.
"""

import json
import logging
import re
from typing import Any

from utils.data_retrieval.clarification import SPACE_CLARIFICATION_QUESTION
from utils.data_retrieval.space_detection import (
    extract_coordinate_radius_filters,
    extract_space_filters,
)
from agents.data_retrieval_project.constants import SPACE_FILTER_FIELD_ORDER, SPACE_OPTION_TO_FIELD

logger = logging.getLogger(__name__)

# Intent Extractor
# ══════════════════════════════════════════════════════════════════════════════

def extract_space_metadata_filters(text: str) -> dict[str, str]:
    """
    Parse UI clarification metadata such as:
      selected_options=city
      additional_details=Pune
    """
    if not text:
        return {}

    selected: list[str] = []
    details = ""
    for raw_line in text.splitlines():
        key, sep, value = raw_line.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "selected_options":
            selected = [
                item.strip().lower()
                for item in re.split(r"[,|]", value)
                if item.strip()
            ]
        elif key in {"additional_details", "other_text"} and value:
            details = value

    if not selected or not details:
        return {}

    filters: dict[str, str] = {}
    for option in selected:
        field = SPACE_OPTION_TO_FIELD.get(option)
        if field:
            filters[field] = details
    return filters


def infer_space_filters(user_query: str) -> dict[str, str]:
    regex_filters, _ = extract_space_filters(user_query, SPACE_FILTER_FIELD_ORDER)
    metadata_filters = extract_space_metadata_filters(user_query)

    filters: dict[str, str] = {}
    filters.update(regex_filters)
    filters.update(metadata_filters)
    if "city" in filters and "city_name" not in filters:
        filters["city_name"] = filters.pop("city")
    return {k: v for k, v in filters.items() if v not in (None, "")}


def merge_space_filters(intent: dict, user_query: str) -> None:
    entities = intent.get("entities")
    if not isinstance(entities, dict):
        entities = {}
        intent["entities"] = entities

    existing = entities.get("space_filters")
    if not isinstance(existing, dict):
        existing = {}

    inferred = infer_space_filters(user_query)
    merged = {
        k: v for k, v in existing.items()
        if isinstance(k, str) and v not in (None, "")
    }
    for field, value in inferred.items():
        merged.setdefault(field, value)

    if merged:
        entities["space_filters"] = merged

    coordinate_filters = extract_coordinate_radius_filters(user_query)
    if coordinate_filters:
        spatial_filters = entities.get("spatial_filters")
        if not isinstance(spatial_filters, dict):
            spatial_filters = {}
        spatial_filters.update(coordinate_filters)
        entities["spatial_filters"] = spatial_filters


def contains_space_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(contains_space_value(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_space_value(item) for item in value)
    return value not in (None, "", [], {})


def contains_named_entity(value: Any) -> bool:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if contains_space_value(item.get("value") or item.get("name")):
                    return True
            elif contains_space_value(item):
                return True
        return False
    if isinstance(value, dict):
        return contains_space_value(value.get("value") or value.get("name"))
    return contains_space_value(value)


def intent_has_space_context(intent: dict) -> bool:
    entities = intent.get("entities") or {}
    if has_coordinate_space_context(entities.get("spatial_filters")):
        return True
    if contains_named_entity(entities.get("locations")):
        return True
    if contains_named_entity(entities.get("projects")):
        return True
    if contains_space_value(entities.get("space_filters")):
        return True

    legacy_filters = entities.get("filters") if isinstance(entities, dict) else None
    if contains_space_value(legacy_filters):
        for field in SPACE_FILTER_FIELD_ORDER:
            if contains_space_value((legacy_filters or {}).get(field)):
                return True

    extra_filters = (intent.get("filters") or {}).get("extra")
    if isinstance(extra_filters, list):
        for item in extra_filters:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or item.get("column") or "").lower()
            if field in SPACE_FILTER_FIELD_ORDER or field in SPACE_OPTION_TO_FIELD.values():
                if contains_space_value(item.get("value")):
                    return True

    return False


def has_coordinate_space_context(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        lat = float(value.get("latitude"))
        lon = float(value.get("longitude"))
    except (TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def mark_space_clarification_required(intent: dict) -> None:
    intent["route"] = "clarify"
    intent["needs_clarification"] = True
    intent["clarification_reason"] = (
        "I need to know which space or geography to filter before querying project data."
    )
    intent["clarification_questions"] = [SPACE_CLARIFICATION_QUESTION]


# Module-level pure helpers
# ══════════════════════════════════════════════════════════════════════════════

def clean_sql(sql: str) -> str:
    """Strip markdown fences that LLMs occasionally emit."""
    sql = sql.strip()
    sql = re.sub(r"^```sql\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"^```\s*",    "", sql)
    sql = re.sub(r"\s*```$",    "", sql)
    return sql.strip()


def validate_select_only(sql: str) -> str:
    """
    Raise ValueError if the SQL is not a SELECT/WITH query or contains
    any blocked DML/DDL keyword. Returns unchanged SQL if valid.
    """
    sql_lower = sql.strip().lower()
    blocked = re.compile(
        r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke)\b",
        re.IGNORECASE,
    )
    if not (sql_lower.startswith("select") or sql_lower.startswith("with")):
        raise ValueError(
            f"Generated SQL is not a SELECT/WITH query. Got: {sql[:80]}"
        )
    if blocked.search(sql_lower):
        raise ValueError("Generated SQL contains a blocked DML/DDL keyword.")
    return sql


def parse_json(text: str, default: Any) -> Any:
    """
    Robustly parse JSON from an LLM response.
    Strips markdown fences, tries full parse, then extracts first {...}.
    Returns default on all failures.
    """
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```$",        "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    logger.warning(
        "parse_json: could not parse LLM response — using default. Preview: %s",
        text[:200],
    )
    return default


def extract_filter_columns(sql: str) -> list[str]:
    """
    Extract column names from WHERE-clause filter expressions.
    """
    patterns = [
        r"\b(\w+)\s+ILIKE\s+",
        r"\b(\w+)\s*=\s*'",
        r"\b(\w+)\s+IN\s+\(",
        r"\b(\w+)\s+BETWEEN\s+",
        r"\b(\w+)\s+IS\s+(?:NOT\s+)?NULL",
        r"\b(\w+)\s*[><=!]+\s*\d",
    ]
    cols: list[str] = []
    for pattern in patterns:
        cols.extend(re.findall(pattern, sql, re.IGNORECASE))

    sql_keywords = {
        "where", "and", "or", "not", "on", "join", "having",
        "case", "when", "then", "else", "end", "select", "from",
        "null", "true", "false",
    }
    return list({c.lower() for c in cols if c.lower() not in sql_keywords})
