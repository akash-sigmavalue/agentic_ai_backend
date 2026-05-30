"""Build structured clarification payloads for the data retrieval v2 SSE UI."""

from __future__ import annotations

from typing import Any

from .semantic_defaults import DEFAULT_DISTINCT_DATABASE_VALUES

DROPDOWN_FIELDS = frozenset({"property_type", "transaction_category", "unit_configuration", "project_type"})

TEXT_FIELDS: dict[str, dict[str, str]] = {
    "city_name": {
        "label": "City",
        "placeholder": "e.g. Pune, Mumbai, Bengaluru",
    },
    "location_name": {
        "label": "Location / locality",
        "placeholder": "e.g. Baner, Andheri West",
    },
    "micro_market": {
        "label": "Micro market",
        "placeholder": "e.g. Hinjewadi Phase 1",
    },
    "time_period": {
        "label": "Time period",
        "placeholder": "e.g. 2024 Q1, January 2024, last 6 months",
    },
    "time": {
        "label": "Time period",
        "placeholder": "e.g. 2024 Q1, January 2024, last 6 months",
    },
}


def _normalize_field_name(raw: str) -> str:
    name = str(raw or "").strip().lower().replace(" ", "_")
    if name.startswith("filters."):
        name = name.split(".", 1)[1]
    if name.startswith("entities."):
        name = name.split(".", 1)[1]
    return name


def _field_definition(output: dict[str, Any], field: str) -> str:
    definitions = output.get("field_definitions")
    if not isinstance(definitions, dict):
        return ""
    for key, value in definitions.items():
        if _normalize_field_name(key) == field and isinstance(value, str):
            return value
    return ""


def _dropdown_field(field: str) -> dict[str, Any]:
    options = DEFAULT_DISTINCT_DATABASE_VALUES.get(field) or []
    label = field.replace("_", " ").title()
    return {
        "field": field,
        "type": "select",
        "label": label,
        "placeholder": f"Select {label.lower()}",
        "options": [{"value": value, "label": value} for value in options],
        "required": True,
    }


def _text_field(field: str, hint: str = "") -> dict[str, Any]:
    meta = TEXT_FIELDS.get(field, {})
    label = meta.get("label") or field.replace("_", " ").title()
    placeholder = meta.get("placeholder") or hint or f"Enter {label.lower()}"
    return {
        "field": field,
        "type": "text",
        "label": label,
        "placeholder": placeholder,
        "required": True,
    }


def _textarea_field(
    field: str,
    *,
    label: str,
    placeholder: str,
    question: str = "",
    options: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "field": field,
        "type": "textarea",
        "label": label,
        "placeholder": placeholder,
        "required": True,
    }
    if question:
        payload["help_text"] = question
    if options:
        payload["options"] = [{"value": option, "label": option} for option in options]
    return payload


def _fields_from_missing_list(output: dict[str, Any], missing_fields: list[Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for raw in missing_fields:
        field = _normalize_field_name(str(raw))
        if not field or any(existing["field"] == field for existing in fields):
            continue
        hint = _field_definition(output, field)
        if field in DROPDOWN_FIELDS:
            field_payload = _dropdown_field(field)
            if hint:
                field_payload["help_text"] = hint
            fields.append(field_payload)
        elif field in TEXT_FIELDS or field.endswith("_name") or "time" in field:
            field_payload = _text_field(field, hint)
            if hint:
                field_payload["help_text"] = hint
            fields.append(field_payload)
        else:
            fields.append(
                _textarea_field(
                    field,
                    label=field.replace("_", " ").title(),
                    placeholder=hint or f"Provide {field.replace('_', ' ')}",
                    question=hint,
                )
            )
    return fields


def _infer_stage_1_fields(output: dict[str, Any], question: str) -> list[dict[str, Any]]:
    missing = output.get("missing_fields")
    if isinstance(missing, list) and missing:
        return _fields_from_missing_list(output, missing)

    inferred: list[str] = []
    lowered = question.lower()
    for field in ("city_name", "location_name", "micro_market", "time_period", "time", "property_type", "transaction_category"):
        if field.replace("_", " ") in lowered or field in lowered:
            inferred.append(field)
    if inferred:
        return _fields_from_missing_list(output, inferred)
    return []


def _fields_for_stage_1_5(output: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    checks = output.get("metric_meaning_checks")
    if not isinstance(checks, list):
        return fields
    for index, item in enumerate(checks):
        if not isinstance(item, dict):
            continue
        if item.get("meaning_status") != "vague" and not item.get("clarification_required"):
            continue
        metric = str(item.get("metric") or f"metric_{index + 1}")
        field = f"metric_meaning__{metric}"
        possible = [str(value) for value in (item.get("possible_meanings") or []) if value]
        fields.append(
            _textarea_field(
                field,
                label=f"Metric: {metric}",
                placeholder="Describe what this metric should mean for your question",
                question=str(item.get("clarification_question") or ""),
                options=possible or None,
            )
        )
    return fields


def _fields_for_stage_1_6(output: dict[str, Any], question: str) -> list[dict[str, Any]]:
    relationship = output.get("metric_relationship")
    help_text = ""
    if isinstance(relationship, dict):
        help_text = str(relationship.get("clarification_question") or "")
    if not help_text:
        help_text = question
    if not help_text:
        return []
    return [
        _textarea_field(
            "metric_relationship",
            label="Metric calculation relationship",
            placeholder="Explain whether metrics should be combined or returned separately",
            question=help_text,
        )
    ]


def _fields_for_algorithm_stage(output: dict[str, Any], question: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for collection_name, prefix in (
        ("calculation_logic_validation", "calculation"),
        ("column_mapping_decisions", "column_mapping"),
    ):
        collection = output.get(collection_name)
        if not isinstance(collection, list):
            continue
        for index, item in enumerate(collection):
            if not isinstance(item, dict):
                continue
            status = str(item.get("validation_status") or item.get("mapping_status") or "")
            clarification = str(item.get("clarification_required") or "")
            if status != "needs_clarification" and not clarification:
                continue
            metric = str(item.get("metric_name") or item.get("metric") or f"{prefix}_{index + 1}")
            field = f"{prefix}__{metric}"
            fields.append(
                _textarea_field(
                    field,
                    label=metric.replace("_", " ").title(),
                    placeholder="Provide the calculation or column mapping you want",
                    question=clarification or question,
                )
            )
    if fields:
        return fields
    if question:
        return [
            _textarea_field(
                "algorithm_clarification",
                label="Clarification",
                placeholder="Answer the question below so the pipeline can continue",
                question=question,
            )
        ]
    return []


def build_clarification_fields(stage_name: str, output: dict[str, Any], question: str) -> list[dict[str, Any]]:
    if stage_name == "stage_1":
        fields = _infer_stage_1_fields(output, question)
        if fields:
            return fields
    if stage_name == "stage_1_5":
        fields = _fields_for_stage_1_5(output)
        if fields:
            return fields
    if stage_name == "stage_1_6":
        fields = _fields_for_stage_1_6(output, question)
        if fields:
            return fields
    if stage_name in {"stage_2", "stage_2_1", "stage_3", "stage_3_1"}:
        fields = _fields_for_algorithm_stage(output, question)
        if fields:
            return fields

    if question:
        return [
            _textarea_field(
                "clarification_answer",
                label="Your answer",
                placeholder="Provide the missing detail",
                question=question,
            )
        ]
    return []


def build_sse_clarification_payload(response: Any) -> dict[str, Any]:
    """Shape clarification metadata for the existing data retrieval SSE consumer."""
    stage_output: dict[str, Any] = {}
    stages = getattr(response, "stages", None) or {}
    stopped_at = getattr(response, "stopped_at_stage", "") or ""
    if stopped_at and isinstance(stages, dict):
        stage_output = stages.get(stopped_at) or {}

    question = getattr(response, "clarification_question", "") or ""
    fields = getattr(response, "clarification_fields", None)
    if fields is None:
        fields = build_clarification_fields(stopped_at, stage_output, question)

    display_question = question or "Please clarify the requested values."
    return {
        "message": display_question,
        "questions": [display_question],
        "clarification_question": display_question,
        "clarification_type": "v2_pipeline",
        "original_query": getattr(response, "query", "") or "",
        "stopped_at_stage": stopped_at,
        "next_action": getattr(response, "next_action", "") or "",
        "fields": fields,
        "missing_fields": stage_output.get("missing_fields") if isinstance(stage_output.get("missing_fields"), list) else [],
    }
