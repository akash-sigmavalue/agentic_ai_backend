import re
from typing import Any


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def semantic_field_name(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        for key in ("name", "field", "key", "id", "label", "title", "column"):
            field_value = value.get(key)
            if field_value is not None and str(field_value).strip():
                value = field_value
                break
        else:
            return ""

    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", str(value).strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def plain_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, dict):
        for key in ("title", "name", "label", "value", "text"):
            item = value.get(key)
            if item is not None and str(item).strip():
                return str(item).strip()
        return default
    return str(value).strip() or default


def normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text_value = str(value).strip().lower()
    if text_value in {"true", "yes", "y", "1", "enabled"}:
        return True
    if text_value in {"false", "no", "n", "0", "disabled"}:
        return False
    return default


def normalize_int(value: Any) -> int | None:
    if value in (None, "", [], {}):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_sort_rules(value: Any) -> list[dict[str, str]]:
    sort_rules = []
    for item in as_list(value):
        if isinstance(item, dict):
            field_name = semantic_field_name(item.get("field") or item.get("name") or item.get("column"))
            order = str(item.get("order") or item.get("direction") or "asc").lower()
        else:
            field_name = semantic_field_name(item)
            order = "asc"

        if not field_name:
            continue
        if order not in {"asc", "desc"}:
            order = "asc"
        sort_rules.append({"field": field_name, "order": order})
    return sort_rules


def normalize_aggregation_rule(value: Any) -> dict[str, str] | None:
    if value in (None, "", [], {}):
        return None

    if isinstance(value, dict):
        field_name = semantic_field_name(value.get("field") or value.get("column") or value.get("name"))
        metric = str(value.get("metric") or value.get("operation") or value.get("function") or "sum").lower()
    else:
        field_name = semantic_field_name(value)
        metric = "sum"

    if not field_name:
        return None
    return {"metric": metric, "field": field_name}


def normalize_semantic_schema_shape(schema_dict: dict) -> dict:
    schema = dict(schema_dict or {})
    schema["type"] = plain_text(schema.get("type"), "dashboard")
    schema["title"] = plain_text(schema.get("title"), "Semantic Data View")
    schema["layout"] = plain_text(schema.get("layout"), "grid")
    schema["primary_data_source"] = plain_text(schema.get("primary_data_source"), "semantic_source")
    schema["user_intent"] = plain_text(schema.get("user_intent"))
    schema["data_source"] = plain_text(schema.get("data_source"), "semantic_source")

    for key in ("columns", "group_by"):
        schema[key] = [name for name in (semantic_field_name(item) for item in as_list(schema.get(key))) if name]

    schema["sort_by"] = normalize_sort_rules(schema.get("sort_by"))
    schema["aggregation"] = normalize_aggregation_rule(schema.get("aggregation"))
    schema["pagination"] = normalize_bool(schema.get("pagination"))
    schema["sortable"] = normalize_bool(schema.get("sortable"))
    schema["category_field"] = semantic_field_name(schema.get("category_field")) or None
    schema["value_field"] = semantic_field_name(schema.get("value_field")) or None
    schema["required_metrics"] = [name for name in (semantic_field_name(item) for item in as_list(schema.get("required_metrics"))) if name]
    schema["charts"] = [name for name in (semantic_field_name(item) for item in as_list(schema.get("charts"))) if name]
    schema["tables"] = [name for name in (semantic_field_name(item) for item in as_list(schema.get("tables"))) if name]
    schema["filters"] = as_dict(schema.get("filters"))

    components = []
    for component in as_list(schema.get("components")):
        comp = dict(component or {})
        comp["type"] = plain_text(comp.get("type"), "table")
        comp["title"] = plain_text(comp.get("title"), "Untitled Component")
        comp["data_source"] = plain_text(comp.get("data_source"), "semantic_source")
        comp["intent"] = plain_text(comp.get("intent"), "analysis")
        for key in ("columns", "group_by"):
            comp[key] = [name for name in (semantic_field_name(item) for item in as_list(comp.get(key))) if name]
        comp["filters"] = as_dict(comp.get("filters"))
        comp["sort_by"] = normalize_sort_rules(comp.get("sort_by"))
        comp["aggregation"] = normalize_aggregation_rule(comp.get("aggregation"))
        comp["limit"] = normalize_int(comp.get("limit"))
        comp["pagination"] = normalize_bool(comp.get("pagination"))
        comp["sortable"] = normalize_bool(comp.get("sortable"))
        comp["category_field"] = semantic_field_name(comp.get("category_field")) or None
        comp["value_field"] = semantic_field_name(comp.get("value_field")) or None
        components.append(comp)

    if not components and schema.get("columns"):
        components = [
            {
                "type": schema.get("type") or "table",
                "title": schema.get("title") or "Semantic Data View",
                "data_source": schema.get("data_source") or "semantic_source",
                "intent": "analysis",
                "columns": schema.get("columns", []),
                "filters": schema.get("filters", {}),
                "group_by": schema.get("group_by", []),
                "aggregation": schema.get("aggregation"),
                "sort_by": schema.get("sort_by", []),
                "limit": None,
                "pagination": schema.get("pagination", False),
                "sortable": schema.get("sortable", False),
                "category_field": schema.get("category_field"),
                "value_field": schema.get("value_field"),
            }
        ]
    schema["components"] = components
    return schema


def normalize_resolver_shape(resolver_dict: dict) -> dict:
    resolver = dict(resolver_dict or {})
    resolver["can_answer"] = normalize_bool(resolver.get("can_answer"), True)
    reason = resolver.get("reason")
    resolver["reason"] = None if reason in (None, "", [], {}) else str(reason)
    resolver["grounded_schema"] = resolver.get("grounded_schema") if isinstance(resolver.get("grounded_schema"), dict) else {}
    resolver["semantic_mapping"] = resolver.get("semantic_mapping") if isinstance(resolver.get("semantic_mapping"), dict) else {}

    sql_queries = resolver.get("sql_queries")
    if isinstance(sql_queries, str):
        resolver["sql_queries"] = [sql_queries] if sql_queries.strip() else []
    else:
        resolver["sql_queries"] = [str(query).strip() for query in as_list(sql_queries) if str(query).strip()]

    if resolver["can_answer"] and (not resolver["grounded_schema"] or not resolver["semantic_mapping"]):
        resolver["can_answer"] = False
        resolver["reason"] = resolver["reason"] or "Resolver returned an incomplete grounding result."
        resolver["sql_queries"] = []
    return resolver


def replace_schema_fields(value: Any, mapping: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return str(mapping.get(value, value))
    if isinstance(value, list):
        return [replace_schema_fields(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: replace_schema_fields(item, mapping) for key, item in value.items()}
    return value


def ground_schema_from_mapping(semantic_schema_dict: dict, semantic_mapping: dict) -> dict:
    mapping = {
        str(key): str(value)
        for key, value in (semantic_mapping or {}).items()
        if value is not None and str(value).strip()
    }
    if not mapping:
        return {}
    return replace_schema_fields(semantic_schema_dict, mapping)
