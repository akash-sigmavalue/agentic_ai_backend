from __future__ import annotations

from typing import Any
import re

from pydantic import BaseModel, Field, field_validator, model_validator


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _field_name(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        for key in ("field", "name", "column", "key", "id", "label", "title"):
            item = value.get(key)
            if item is not None and str(item).strip():
                value = item
                break
        else:
            return ""
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", str(value).strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _string_list(value: Any, semantic: bool = True) -> list[str]:
    items = []
    for item in _as_list(value):
        text = _field_name(item) if semantic else str(item).strip()
        if text:
            items.append(text)
    return items


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"true", "yes", "y", "1", "enabled"}


class SortRule(BaseModel):
    field: str
    order: str = "asc"

    @model_validator(mode="before")
    @classmethod
    def normalize_sort_rule(cls, value: Any) -> dict:
        if isinstance(value, dict):
            field = _field_name(value.get("field") or value.get("name") or value.get("column"))
            order = str(value.get("order") or value.get("direction") or "asc").lower()
        else:
            field = _field_name(value)
            order = "asc"
        if order not in {"asc", "desc"}:
            order = "asc"
        return {"field": field, "order": order}


class AggregationRule(BaseModel):
    metric: str
    field: str

    @model_validator(mode="before")
    @classmethod
    def normalize_aggregation_rule(cls, value: Any) -> dict:
        if isinstance(value, dict):
            metric = str(value.get("metric") or value.get("operation") or value.get("function") or "sum").lower()
            field = _field_name(value.get("field") or value.get("column") or value.get("name"))
        else:
            metric = "sum"
            field = _field_name(value)
        return {"metric": metric, "field": field}


class ComponentSchema(BaseModel):
    type: str
    title: str
    data_source: str = "semantic_source"
    intent: str = "detailed_records"
    columns: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list)
    aggregation: AggregationRule | None = None
    sort_by: list[SortRule] = Field(default_factory=list)
    limit: int | None = None
    pagination: bool = False
    sortable: bool = False
    category_field: str | None = None
    value_field: str | None = None

    @field_validator("columns", "group_by", mode="before")
    @classmethod
    def normalize_field_lists(cls, value: Any) -> list[str]:
        return _string_list(value)

    @field_validator("sort_by", mode="before")
    @classmethod
    def normalize_sort_list(cls, value: Any) -> list:
        return _as_list(value)

    @field_validator("filters", mode="before")
    @classmethod
    def normalize_filters(cls, value: Any) -> dict:
        return _as_dict(value)

    @field_validator("pagination", "sortable", mode="before")
    @classmethod
    def normalize_booleans(cls, value: Any) -> bool:
        return _as_bool(value)

    @field_validator("category_field", "value_field", mode="before")
    @classmethod
    def normalize_optional_fields(cls, value: Any) -> str | None:
        field = _field_name(value)
        return field or None


class PlannerSchemaOut(BaseModel):
    type: str = "dashboard"
    title: str
    layout: str = "grid"
    primary_data_source: str = "semantic_source"
    user_intent: str = ""
    data_source: str | None = "semantic_source"
    columns: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list)
    aggregation: AggregationRule | None = None
    sort_by: list[SortRule] = Field(default_factory=list)
    pagination: bool = False
    sortable: bool = False
    category_field: str | None = None
    value_field: str | None = None
    required_metrics: list[str] = Field(default_factory=list)
    charts: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    components: list[ComponentSchema] = Field(default_factory=list)

    @field_validator("columns", "group_by", "required_metrics", "charts", "tables", mode="before")
    @classmethod
    def normalize_field_lists(cls, value: Any) -> list[str]:
        return _string_list(value)

    @field_validator("sort_by", mode="before")
    @classmethod
    def normalize_sort_list(cls, value: Any) -> list:
        return _as_list(value)

    @field_validator("filters", mode="before")
    @classmethod
    def normalize_filters(cls, value: Any) -> dict:
        return _as_dict(value)

    @field_validator("pagination", "sortable", mode="before")
    @classmethod
    def normalize_booleans(cls, value: Any) -> bool:
        return _as_bool(value)

    @field_validator("category_field", "value_field", mode="before")
    @classmethod
    def normalize_optional_fields(cls, value: Any) -> str | None:
        field = _field_name(value)
        return field or None


class DBResolverPlanOut(BaseModel):
    can_answer: bool = True
    reason: str | None = None
    grounded_schema: dict[str, Any] = Field(default_factory=dict)
    semantic_mapping: dict[str, Any] = Field(default_factory=dict)
    sql_queries: list[str] = Field(default_factory=list)

    @field_validator("can_answer", mode="before")
    @classmethod
    def normalize_can_answer(cls, value: Any) -> bool:
        return _as_bool(value)

    @field_validator("grounded_schema", "semantic_mapping", mode="before")
    @classmethod
    def normalize_dicts(cls, value: Any) -> dict:
        return _as_dict(value)

    @field_validator("sql_queries", mode="before")
    @classmethod
    def normalize_sql_queries(cls, value: Any) -> list[str]:
        return [str(item).strip() for item in _as_list(value) if str(item).strip()]
