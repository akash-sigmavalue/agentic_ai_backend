"""
Semantic resolver for project, location, and city names.

This module resolves user-entered names such as misspelled projects,
locations, and city aliases to exact values already present in the database.
It is designed to run before SQL probing/building so the rest of the data
retrieval pipeline can keep using the normal intent shape.
"""

from __future__ import annotations

import difflib
import logging
import os
import threading
from typing import Any, Callable

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

DEFAULT_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:1234@localhost:5432/3_april",
)

PROJECT_TABLE = "projects"
TRANSACTION_TABLE = "transactions"

ENTITY_COLUMN_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    PROJECT_TABLE: {
        "projects": ("project_name", "registered_project_name"),
        "locations": ("location_name", "sub_locality", "micro_market"),
        "cities": ("city_name",),
    },
    TRANSACTION_TABLE: {
        "projects": ("project_name",),
        "locations": ("location_name", "sub_locality", "micro_market", "village_name"),
        "cities": ("city_name",),
    },
}

NOISE_WORDS = {
    "project",
    "projects",
    "location",
    "locations",
    "locality",
    "micromarket",
    "micro",
    "market",
    "city",
    "price",
    "rate",
    "units",
    "tower",
}

CITY_ALIASES = {
    "bombay": "mumbai",
    "bombey": "mumbai",
    "bambay": "mumbai",
    "mumbay": "mumbai",
    "poona": "pune",
    "pne": "pune",
}

SPACE_FIELD_TO_CATEGORY = {
    "project_name": "projects",
    "registered_project_name": "projects",
    "location_name": "locations",
    "sub_locality": "locations",
    "micro_market": "locations",
    "village_name": "locations",
    "city": "cities",
    "city_name": "cities",
}

_resolution_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def _normalize(value: Any, category: str | None = None) -> str:
    tokens = str(value or "").strip().split()
    cleaned = " ".join(
        token for token in tokens if token.lower() not in NOISE_WORDS
    ).lower()
    if category == "cities":
        cleaned = CITY_ALIASES.get(cleaned, cleaned)
    return cleaned


def _compress(value: str) -> str:
    return value.replace(" ", "").replace("-", "").replace("_", "")


def _clean_values(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        text_value = str(value).strip()
        key = text_value.lower()
        if not text_value or key in seen:
            continue
        cleaned.append(text_value)
        seen.add(key)
    return cleaned


def _ensure_supported(table_name: str) -> str:
    if table_name not in ENTITY_COLUMN_GROUPS:
        raise ValueError(f"Unsupported entity resolution table: {table_name}")
    return table_name


def _fetch_distinct_values_with_executor(
    table_name: str,
    columns: tuple[str, ...],
    db_executor: Callable[[str], list[dict]],
) -> list[str]:
    values: list[Any] = []
    for column in columns:
        sql = (
            f'SELECT DISTINCT "{column}" AS value '
            f'FROM "{table_name}" '
            f'WHERE "{column}" IS NOT NULL AND length(trim("{column}"::text)) > 1 '
            f'ORDER BY "{column}"'
        )
        try:
            rows = db_executor(sql) or []
            values.extend(row.get("value") for row in rows)
        except Exception as exc:
            logger.warning(
                "Entity resolver could not load %s.%s: %s",
                table_name,
                column,
                exc,
            )
    return _clean_values(values)


def _fetch_distinct_values_with_engine(
    table_name: str,
    columns: tuple[str, ...],
    db_url: str | None = None,
) -> list[str]:
    values: list[Any] = []
    engine = create_engine(db_url or DEFAULT_DB_URL)
    with engine.connect() as conn:
        for column in columns:
            sql = text(
                f'SELECT DISTINCT "{column}" AS value '
                f'FROM "{table_name}" '
                f'WHERE "{column}" IS NOT NULL AND length(trim("{column}"::text)) > 1 '
                f'ORDER BY "{column}"'
            )
            try:
                values.extend(row[0] for row in conn.execute(sql).fetchall())
            except Exception as exc:
                logger.warning(
                    "Entity resolver could not load %s.%s: %s",
                    table_name,
                    column,
                    exc,
                )
    return _clean_values(values)


def _build_category_cache(values: list[str]) -> dict[str, Any]:
    mapping = {value.lower(): value for value in values}
    return {
        "maps": mapping,
        "possibilities": list(mapping.keys()),
        "compressed": {_compress(key): value for key, value in mapping.items()},
    }


def load_entity_cache(
    table_name: str,
    db_executor: Callable[[str], list[dict]] | None = None,
    db_url: str | None = None,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """
    Load distinct project/location/city values for a supported table.

    Results are cached per table. When the agent provides db_executor, this uses
    the application's existing database path; otherwise it falls back to the
    hardcoded/default SQLAlchemy connection for local use.
    """
    table_name = _ensure_supported(table_name)
    cache_key = table_name

    if not force_refresh and cache_key in _resolution_cache:
        return _resolution_cache[cache_key]

    with _cache_lock:
        if not force_refresh and cache_key in _resolution_cache:
            return _resolution_cache[cache_key]

        category_cache: dict[str, dict[str, Any]] = {}
        for category, columns in ENTITY_COLUMN_GROUPS[table_name].items():
            if db_executor is not None:
                values = _fetch_distinct_values_with_executor(
                    table_name=table_name,
                    columns=columns,
                    db_executor=db_executor,
                )
            else:
                values = _fetch_distinct_values_with_engine(
                    table_name=table_name,
                    columns=columns,
                    db_url=db_url,
                )
            category_cache[category] = _build_category_cache(values)

        _resolution_cache[cache_key] = category_cache
        return category_cache


def resolve_entity(
    value: Any,
    category: str,
    table_name: str = PROJECT_TABLE,
    db_executor: Callable[[str], list[dict]] | None = None,
    db_url: str | None = None,
    cutoff: float = 0.65,
) -> str:
    """
    Resolve one project/location/city value to the exact DB spelling.

    category must be one of: "projects", "locations", or "cities".
    """
    if value in (None, ""):
        return value

    table_name = _ensure_supported(table_name)
    cache = load_entity_cache(
        table_name=table_name,
        db_executor=db_executor,
        db_url=db_url,
    )
    category_data = cache.get(category) or {}
    mapping: dict[str, str] = category_data.get("maps", {})
    compressed: dict[str, str] = category_data.get("compressed", {})
    possibilities: list[str] = category_data.get("possibilities", [])

    value_clean = _normalize(value, category=category)
    if not value_clean:
        return str(value)

    if value_clean in mapping:
        return mapping[value_clean]

    value_compressed = _compress(value_clean)
    if value_compressed in compressed:
        return compressed[value_compressed]

    matches = difflib.get_close_matches(
        value_clean,
        possibilities,
        n=1,
        cutoff=cutoff,
    )
    if matches:
        return mapping[matches[0]]

    tokens = value_clean.split()
    if len(tokens) > 1:
        for size in range(len(tokens), 0, -1):
            for start in range(len(tokens) - size + 1):
                window = " ".join(tokens[start : start + size])
                if len(window) < 3:
                    continue
                window_matches = difflib.get_close_matches(
                    window,
                    possibilities,
                    n=1,
                    cutoff=0.85,
                )
                if window_matches:
                    return mapping[window_matches[0]]

    return str(value)


def _resolve_text(
    value: Any,
    category: str,
    table_name: str,
    db_executor: Callable[[str], list[dict]] | None,
) -> tuple[str, bool, str]:
    original = str(value).strip()
    resolved = resolve_entity(
        value=original,
        category=category,
        table_name=table_name,
        db_executor=db_executor,
    )
    return resolved, resolved.strip() != original, category


def _resolve_text_with_fallbacks(
    value: Any,
    categories: tuple[str, ...],
    table_name: str,
    db_executor: Callable[[str], list[dict]] | None,
) -> tuple[str, bool, str]:
    """
    Resolve a text value across likely entity categories.

    Intent extraction can misclassify project names as locations. Prefer the
    extractor's category, but if that produces no change, try the adjacent
    categories so a project like "vtp bellesimo" can still become the exact
    project_name "Vtp Bellissimo".
    """
    original = str(value).strip()
    for category in categories:
        resolved, changed, resolved_category = _resolve_text(
            original,
            category,
            table_name,
            db_executor,
        )
        if changed:
            return resolved, changed, resolved_category
    return original, False, categories[0]


def _fallback_categories(category: str) -> tuple[str, ...]:
    if category == "locations":
        return ("locations", "projects", "cities")
    if category == "projects":
        return ("projects", "locations", "cities")
    if category == "cities":
        return ("cities", "locations", "projects")
    return (category,)


def _resolve_named_entity_list(
    items: Any,
    category: str,
    table_name: str,
    db_executor: Callable[[str], list[dict]] | None,
    path: str,
    audit: list[dict[str, str]],
) -> None:
    if not isinstance(items, list):
        return

    for index, item in enumerate(items):
        if isinstance(item, dict):
            raw = item.get("value") or item.get("name")
            if raw in (None, ""):
                continue

            item_category = category
            semantic_level = str(item.get("semantic_level") or "").lower()
            if semantic_level == "city":
                item_category = "cities"
            elif semantic_level == "project":
                item_category = "projects"

            resolved, changed, resolved_category = _resolve_text_with_fallbacks(
                raw,
                _fallback_categories(item_category),
                table_name,
                db_executor,
            )
            if changed:
                item["value"] = resolved
                audit.append({
                    "path": path,
                    "category": resolved_category,
                    "original": str(raw),
                    "resolved": resolved,
                })
        elif item not in (None, ""):
            resolved, changed, resolved_category = _resolve_text_with_fallbacks(
                item,
                _fallback_categories(category),
                table_name,
                db_executor,
            )
            if changed:
                items[index] = resolved
                audit.append({
                    "path": path,
                    "category": resolved_category,
                    "original": str(item),
                    "resolved": resolved,
                })


def resolve_intent_space_entities(
    intent: dict,
    table_name: str,
    db_executor: Callable[[str], list[dict]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """
    Resolve project/location/city entity names in-place on the intent.

    Returns a grouped audit trail, and also writes it to:
      intent["semantic_resolved_entities"]
    """
    _ensure_supported(table_name)
    entities = intent.get("entities")
    if not isinstance(entities, dict):
        intent.setdefault("semantic_resolved_entities", {})
        return {}

    audit: list[dict[str, str]] = []

    _resolve_named_entity_list(
        entities.get("locations"),
        category="locations",
        table_name=table_name,
        db_executor=db_executor,
        path="entities.locations",
        audit=audit,
    )
    _resolve_named_entity_list(
        entities.get("projects"),
        category="projects",
        table_name=table_name,
        db_executor=db_executor,
        path="entities.projects",
        audit=audit,
    )

    project_matches_from_locations = {
        item["resolved"].lower()
        for item in audit
        if item.get("path") == "entities.locations"
        and item.get("category") == "projects"
        and item.get("resolved")
    }
    if project_matches_from_locations:
        locations = entities.get("locations")
        projects = entities.get("projects")
        if isinstance(locations, list):
            if not isinstance(projects, list):
                projects = []
                entities["projects"] = projects

            kept_locations = []
            existing_projects = {
                str(
                    item.get("value") if isinstance(item, dict) else item
                ).strip().lower()
                for item in projects
                if item not in (None, "")
            }

            for item in locations:
                value = (
                    item.get("value")
                    if isinstance(item, dict)
                    else item
                )
                value_text = str(value or "").strip()
                if value_text.lower() not in project_matches_from_locations:
                    kept_locations.append(item)
                    continue

                if value_text.lower() not in existing_projects:
                    projects.append({
                        "value": value_text,
                        "semantic_level": "project",
                    })
                    existing_projects.add(value_text.lower())

            entities["locations"] = kept_locations

    space_filters = entities.get("space_filters")
    if isinstance(space_filters, dict):
        for field, raw in list(space_filters.items()):
            category = SPACE_FIELD_TO_CATEGORY.get(str(field))
            if not category or raw in (None, "", [], {}):
                continue
            if isinstance(raw, list):
                resolved_values = []
                for value in raw:
                    resolved, changed, resolved_category = _resolve_text_with_fallbacks(
                        value,
                        _fallback_categories(category),
                        table_name,
                        db_executor,
                    )
                    resolved_values.append(resolved)
                    if changed:
                        audit.append({
                            "path": f"entities.space_filters.{field}",
                            "category": resolved_category,
                            "original": str(value),
                            "resolved": resolved,
                        })
                space_filters[field] = resolved_values
            else:
                resolved, changed, resolved_category = _resolve_text_with_fallbacks(
                    raw,
                    _fallback_categories(category),
                    table_name,
                    db_executor,
                )
                if changed:
                    space_filters[field] = resolved
                    audit.append({
                        "path": f"entities.space_filters.{field}",
                        "category": resolved_category,
                        "original": str(raw),
                        "resolved": resolved,
                    })

    grouped: dict[str, list[dict[str, str]]] = {}
    for item in audit:
        grouped.setdefault(item["category"], []).append({
            "path": item["path"],
            "original": item["original"],
            "resolved": item["resolved"],
        })

    intent["semantic_resolved_entities"] = grouped
    return grouped


if __name__ == "__main__":
    for raw, category in [
        ("bombey", "cities"),
        ("mumbay", "cities"),
        ("pne", "cities"),
        ("vsn flra", "projects"),
        ("pmlpe nlkh", "locations"),
    ]:
        print(f"{raw:<15} | {category:<10} | {resolve_entity(raw, category)}")
