from __future__ import annotations

from typing import Any

from agents.shared.schema_helpers import as_dict, as_list


def ensure_component_defaults(component: dict) -> dict:
    comp = dict(component or {})

    comp.setdefault("type", "table")
    comp.setdefault("title", "Untitled Component")
    comp.setdefault("data_source", "semantic_source")
    comp.setdefault("intent", "detailed_records")
    comp.setdefault("columns", [])
    comp.setdefault("filters", {})
    comp.setdefault("group_by", [])
    comp.setdefault("aggregation", None)
    comp.setdefault("sort_by", [])
    comp.setdefault("limit", None)
    comp.setdefault("pagination", False)
    comp.setdefault("sortable", False)
    comp.setdefault("category_field", None)
    comp.setdefault("value_field", None)

    comp["columns"] = as_list(comp.get("columns"))
    comp["filters"] = as_dict(comp.get("filters"))
    comp["group_by"] = as_list(comp.get("group_by"))
    comp["sort_by"] = as_list(comp.get("sort_by"))

    if comp.get("data_source") in {"employees", "uploaded_file", None, ""}:
        comp["data_source"] = "semantic_source"

    comp["columns"] = [str(c).strip() for c in comp["columns"] if str(c).strip()]

    if comp["type"] in {"pie_chart", "bar_chart", "line_chart"} and not comp.get("category_field"):
        comp["category_field"] = comp["columns"][0] if comp["columns"] else None

    if comp["type"] in {"pie_chart", "bar_chart", "line_chart", "scatter_plot"} and not comp.get("value_field"):
        if len(comp["columns"]) > 1:
            comp["value_field"] = comp["columns"][1]
        elif comp["type"] != "pie_chart" and comp["columns"]:
            comp["value_field"] = comp["columns"][0]

    return comp


def apply_schema_overrides(
    user_query: str,
    schema_dict: dict,
    selected_widget: str | None = None,
    default_data_source: str = "semantic_source",
) -> dict:
    schema_dict = as_dict(schema_dict)
    default_title = "Semantic Data View"

    schema_dict.setdefault("type", "dashboard")
    schema_dict.setdefault("title", default_title)
    schema_dict.setdefault("layout", "grid")
    schema_dict.setdefault("primary_data_source", default_data_source)
    schema_dict.setdefault("user_intent", user_query)
    schema_dict.setdefault("components", [])

    if not schema_dict.get("title"):
        schema_dict["title"] = default_title
    if not schema_dict.get("user_intent"):
        schema_dict["user_intent"] = user_query

    if schema_dict.get("primary_data_source") in {"employees", "uploaded_file", None, ""}:
        schema_dict["primary_data_source"] = "semantic_source"
    if schema_dict.get("data_source") in {"employees", "uploaded_file", None, ""}:
        schema_dict["data_source"] = "semantic_source"

    schema_dict.setdefault("required_metrics", [])
    schema_dict.setdefault("charts", [])
    schema_dict.setdefault("tables", [])

    schema_dict["components"] = as_list(schema_dict.get("components"))
    schema_dict["columns"] = as_list(schema_dict.get("columns"))
    schema_dict["filters"] = as_dict(schema_dict.get("filters"))
    schema_dict["group_by"] = as_list(schema_dict.get("group_by"))
    schema_dict["sort_by"] = as_list(schema_dict.get("sort_by"))
    schema_dict["required_metrics"] = as_list(schema_dict.get("required_metrics"))
    schema_dict["charts"] = as_list(schema_dict.get("charts"))
    schema_dict["tables"] = as_list(schema_dict.get("tables"))

    components = [ensure_component_defaults(c) for c in schema_dict.get("components", [])]
    if not components:
        raise RuntimeError("Agent 1 returned a semantic schema without components.")

    components = [ensure_component_defaults(c) for c in components]
    schema_dict["components"] = components

    selected_widget_normalized = (selected_widget or "").strip().lower()
    single_widget_types = {"metric", "table", "bar_chart", "line_chart", "pie_chart", "scatter_plot"}

    if selected_widget_normalized == "dashboard":
        schema_dict["type"] = "dashboard"
    elif selected_widget_normalized in single_widget_types:
        single = dict(components[0])
        single["type"] = selected_widget_normalized
        single["data_source"] = single.get("data_source") or "semantic_source"

        if not single.get("columns"):
            single["columns"] = schema_dict.get("columns", [])

        if selected_widget_normalized in {"bar_chart", "line_chart", "pie_chart"}:
            if not single.get("category_field"):
                single["category_field"] = (
                    schema_dict.get("category_field")
                    or (single["columns"][0] if single.get("columns") else None)
                )
            if not single.get("value_field"):
                single["value_field"] = (
                    schema_dict.get("value_field")
                    or (single["columns"][1] if len(single.get("columns", [])) > 1 else None)
                )
            if single.get("category_field") and not single.get("group_by"):
                single["group_by"] = [single["category_field"]]
            if single.get("value_field") and not single.get("aggregation"):
                single["aggregation"] = {"metric": "count", "field": single["value_field"]}

        if selected_widget_normalized == "scatter_plot" and len(single.get("columns", [])) >= 2:
            single.setdefault("category_field", single["columns"][0])
            single.setdefault("value_field", single["columns"][1])

        if selected_widget_normalized == "metric" and single.get("columns") and not single.get("aggregation"):
            single["aggregation"] = {"metric": "count", "field": single["columns"][0]}
            single["value_field"] = single["columns"][0]

        components = [ensure_component_defaults(single)]
        schema_dict["components"] = components
        schema_dict["type"] = selected_widget_normalized
        schema_dict["data_source"] = components[0]["data_source"]
        schema_dict["columns"] = components[0]["columns"]
        schema_dict["filters"] = components[0]["filters"]
        schema_dict["pagination"] = components[0]["pagination"]
        schema_dict["sortable"] = components[0]["sortable"]
        schema_dict["group_by"] = components[0]["group_by"]
        schema_dict["aggregation"] = components[0]["aggregation"]
        schema_dict["sort_by"] = components[0]["sort_by"]
        schema_dict["category_field"] = components[0].get("category_field")
        schema_dict["value_field"] = components[0].get("value_field")
        schema_dict["charts"] = [selected_widget_normalized] if selected_widget_normalized.endswith("chart") else []
        schema_dict["tables"] = ["table"] if selected_widget_normalized == "table" else []
    elif len(components) == 1:
        single = components[0]
        schema_dict["type"] = single["type"]
        schema_dict["data_source"] = single["data_source"]
        schema_dict["columns"] = single["columns"]
        schema_dict["filters"] = single["filters"]
        schema_dict["pagination"] = single["pagination"]
        schema_dict["sortable"] = single["sortable"]
        schema_dict["group_by"] = single["group_by"]
        schema_dict["aggregation"] = single["aggregation"]
        schema_dict["sort_by"] = single["sort_by"]
        schema_dict["category_field"] = single.get("category_field")
        schema_dict["value_field"] = single.get("value_field")
    else:
        schema_dict["type"] = "dashboard"

    return schema_dict
