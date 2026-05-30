"""Convert v2 stage JSON outputs into UI-friendly stage + step payloads."""

from __future__ import annotations

import json
from typing import Any

from .stage_catalog import base_stage_id, stage_catalog_entry


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= 500 else f"{text[:497]}..."
    return str(value)


def _step(step_id: str, label: str, value: Any, *, status: str = "info", detail: str = "") -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "value": _fmt(value),
        "status": status,
        "detail": detail,
    }


def _status_flag(raw: str, *, good: set[str], bad: set[str]) -> str:
    normalized = str(raw or "").lower()
    if normalized in good:
        return "success"
    if normalized in bad:
        return "warning" if "clarification" in normalized else "error"
    return "info"


def _mapped_schema(output: dict[str, Any]) -> dict[str, Any]:
    mapped = output.get("MAPPED_JSON_SCHEMA")
    return mapped if isinstance(mapped, dict) else {}


def _output_schema(output: dict[str, Any]) -> dict[str, Any]:
    schema = output.get("OUTPUT_JSON_SCHEMA")
    return schema if isinstance(schema, dict) else {}


def _steps_stage_1(output: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if output.get("needs_clarification"):
        steps.append(
            _step(
                "clarification",
                "Clarification required",
                output.get("clarification_question"),
                status="warning",
            )
        )
        missing = output.get("missing_fields")
        if isinstance(missing, list) and missing:
            steps.append(_step("missing_fields", "Missing fields", ", ".join(str(item) for item in missing), status="warning"))
        return steps

    out = _output_schema(output)
    mapped = _mapped_schema(output)
    steps.extend(
        [
            _step("analysis_type", "Analysis type", out.get("analysis_type") or mapped.get("analysis_type")),
            _step("intent", "Intent", out.get("intent") or mapped.get("intent")),
            _step("metrics", "Metrics", out.get("metrics") or mapped.get("metrics")),
            _step("expected_output", "Expected output", out.get("expected output") or mapped.get("expected output")),
        ]
    )
    entities = out.get("entities") if isinstance(out.get("entities"), dict) else {}
    space = entities.get("space_entities") if isinstance(entities.get("space_entities"), dict) else {}
    if space:
        steps.append(_step("space_entities", "Space entities", space))
    mapped_entities = mapped.get("entities") if isinstance(mapped.get("entities"), dict) else {}
    if mapped_entities:
        steps.append(_step("mapped_entities", "Mapped space field", mapped_entities.get("space_field")))
    filters = out.get("filters") if isinstance(out.get("filters"), dict) else mapped.get("filters")
    if isinstance(filters, dict) and filters:
        steps.append(_step("filters", "Filters", filters))
    return steps


def _steps_stage_1_5(output: dict[str, Any]) -> list[dict[str, Any]]:
    status = output.get("metric_completeness_status", "")
    steps = [
        _step(
            "metric_status",
            "Metric completeness status",
            status,
            status=_status_flag(status, good={"complete", "fixed"}, bad={"needs_clarification"}),
        ),
        _step("requested_metrics", "Requested metrics", output.get("metrics_requested_from_user_query")),
        _step("found_metrics", "Found in stage 1", output.get("metrics_found_in_stage_1")),
        _step("missing_metrics", "Missing metrics", output.get("missing_metrics_identified")),
        _step("final_metrics", "Final metrics list", output.get("final_metrics_list")),
    ]
    checks = output.get("metric_meaning_checks")
    if isinstance(checks, list):
        for index, item in enumerate(checks):
            if not isinstance(item, dict):
                continue
            metric = item.get("metric") or f"metric_{index + 1}"
            meaning_status = item.get("meaning_status", "")
            steps.append(
                _step(
                    f"metric_check_{index}",
                    f"Metric meaning · {metric}",
                    item.get("resolved_metric_meaning") or item.get("possible_meanings"),
                    status=_status_flag(str(meaning_status), good={"clear"}, bad={"vague"}),
                    detail=str(item.get("clarification_question") or ""),
                )
            )
    mapped = _mapped_schema(output)
    if mapped.get("metrics"):
        steps.append(_step("mapped_metrics", "Mapped metrics", mapped.get("metrics")))
    return steps


def _steps_stage_1_6(output: dict[str, Any]) -> list[dict[str, Any]]:
    status = output.get("metric_relationship_status", "")
    relationship = output.get("metric_relationship") if isinstance(output.get("metric_relationship"), dict) else {}
    steps = [
        _step(
            "relationship_status",
            "Relationship status",
            status,
            status=_status_flag(status, good={"classified"}, bad={"needs_clarification"}),
        ),
        _step("relationship_type", "Relationship type", relationship.get("relationship_type")),
        _step("relationship_reason", "Reason", relationship.get("reason")),
    ]
    combined = output.get("combined_case_output")
    if isinstance(combined, dict) and combined.get("applicable"):
        steps.append(_step("combined_case", "Combined case", combined))
    individual = output.get("individual_case_output")
    if isinstance(individual, dict) and individual.get("applicable"):
        steps.append(_step("individual_case", "Individual case", individual.get("individual_metrics")))
    return steps


def _algorithm_steps(output: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    status = output.get("algorithm_status", "")
    steps.append(
        _step(
            "algorithm_status",
            "Algorithm status",
            status,
            status=_status_flag(status, good={"ready"}, bad={"needs_clarification", "schema_missing"}),
        )
    )
    for index, item in enumerate(output.get("calculation_logic_validation") or []):
        if not isinstance(item, dict):
            continue
        metric = item.get("metric_name") or f"metric_{index + 1}"
        validation = item.get("validation_status", "")
        steps.append(
            _step(
                f"calc_validation_{index}",
                f"Calculation logic · {metric}",
                validation,
                status=_status_flag(str(validation), good={"approved"}, bad={"needs_clarification"}),
                detail=str(item.get("clarification_required") or ""),
            )
        )
    for index, item in enumerate(output.get("column_mapping_decisions") or []):
        if not isinstance(item, dict):
            continue
        metric = item.get("metric_name") or f"metric_{index + 1}"
        mapping = item.get("mapping_status", "")
        steps.append(
            _step(
                f"column_map_{index}",
                f"Column mapping · {metric}",
                item.get("selected_column") or mapping,
                status=_status_flag(str(mapping), good={"selected"}, bad={"needs_clarification"}),
                detail=str(item.get("selection_reason") or item.get("clarification_required") or ""),
            )
        )
    structure = output.get("final_algorithm_structure")
    if isinstance(structure, dict):
        steps.append(_step("algorithm_structure", "Final algorithm structure", structure))
        combined = structure.get("combined_algorithm")
        if isinstance(combined, dict) and combined.get("applicable"):
            structured = combined.get("structured_steps")
            if structured:
                steps.append(_step("combined_steps", "Combined structured steps", structured))
        for index, algo in enumerate(structure.get("individual_algorithms") or []):
            if isinstance(algo, dict) and algo.get("structured_steps"):
                name = algo.get("metric_name") or f"metric_{index + 1}"
                steps.append(_step(f"individual_steps_{index}", f"Steps · {name}", algo.get("structured_steps")))
    return steps


def _steps_stage_3(output: dict[str, Any]) -> list[dict[str, Any]]:
    status = output.get("sql_build_status", "")
    steps = [
        _step(
            "sql_build_status",
            "SQL build status",
            status,
            status=_status_flag(status, good={"ready"}, bad={"blocked", "needs_clarification"}),
        ),
    ]
    sql = output.get("generated_sql") or output.get("sql_query") or output.get("combined_sql")
    if sql:
        steps.append(_step("generated_sql", "Generated SQL", sql))
    queries = output.get("individual_sql_queries")
    if isinstance(queries, list) and queries:
        steps.append(_step("individual_sql", "Individual SQL queries", queries))
    return steps


def _steps_stage_3_1(output: dict[str, Any]) -> list[dict[str, Any]]:
    status = output.get("sql_review_status", "")
    steps = [
        _step(
            "sql_review_status",
            "SQL review status",
            status,
            status=_status_flag(status, good={"approved"}, bad={"blocked", "needs_clarification"}),
        ),
    ]
    if output.get("review_summary"):
        steps.append(_step("review_summary", "Review summary", output.get("review_summary")))
    if output.get("blocking_issues"):
        steps.append(_step("blocking_issues", "Blocking issues", output.get("blocking_issues"), status="error"))
    if output.get("approved_sql"):
        steps.append(_step("approved_sql", "Approved SQL", output.get("approved_sql")))
    return steps


def _steps_stage_3_2(output: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if output.get("non_execution_reason"):
        steps.append(_step("non_execution", "Execution blocked", output.get("non_execution_reason"), status="error"))
    results = output.get("execution_results")
    if isinstance(results, dict):
        combined = results.get("combined_sql")
        if isinstance(combined, dict):
            steps.append(
                _step(
                    "combined_probe",
                    "Combined SQL probe",
                    f"{combined.get('execution_status')} · {combined.get('row_count', 0)} rows",
                    status=_status_flag(str(combined.get("execution_status")), good={"success"}, bad={"failed"}),
                )
            )
        for index, item in enumerate(results.get("individual_sql_queries") or []):
            if not isinstance(item, dict):
                continue
            metric = item.get("metric_name") or f"query_{index + 1}"
            steps.append(
                _step(
                    f"probe_{index}",
                    f"Probe · {metric}",
                    f"{item.get('execution_status')} · {item.get('row_count', 0)} rows",
                    status=_status_flag(str(item.get("execution_status")), good={"success"}, bad={"failed"}),
                )
            )
    return steps or [_step("probe", "SQL probe", "No execution evidence returned.")]


def _steps_stage_3_3(output: dict[str, Any]) -> list[dict[str, Any]]:
    decision = output.get("react_decision")
    if isinstance(decision, dict):
        decision_value = decision.get("decision")
    else:
        decision_value = output.get("decision")
    steps = [
        _step(
            "observe_status",
            "Observe status",
            output.get("sql_observe_status") or output.get("observe_status"),
        ),
        _step(
            "react_decision",
            "ReAct decision",
            decision_value,
            status=_status_flag(
                str(decision_value),
                good={"stop_success"},
                bad={"stop_no_data", "send_to_sql_fix", "stop_failed"},
            ),
        ),
    ]
    if output.get("observation_summary"):
        steps.append(_step("observation_summary", "Observation summary", output.get("observation_summary")))
    if output.get("evidence"):
        steps.append(_step("evidence", "Evidence", output.get("evidence")))
    return steps


def _steps_stage_3_4(output: dict[str, Any]) -> list[dict[str, Any]]:
    status = output.get("sql_fix_status", "")
    return [
        _step(
            "sql_fix_status",
            "SQL fix status",
            status,
            status=_status_flag(status, good={"fixed"}, bad={"blocked", "failed"}),
        ),
        _step("fix_summary", "Fix summary", output.get("fix_summary") or output.get("summary")),
        _step("corrected_sql", "Corrected SQL", output.get("corrected_sql") or output.get("fixed_sql")),
        _step("send_back", "Send back to review", output.get("send_back_to_sql_review")),
    ]


def _steps_stage_4(output: dict[str, Any]) -> list[dict[str, Any]]:
    steps = [
        _step("layman_output", "Layman output", output.get("layman_output")),
    ]
    insights = output.get("insights")
    if isinstance(insights, list) and insights:
        steps.append(_step("insights", "Insights", insights))
    arrival = output.get("steps_of_arriving_at_output")
    if isinstance(arrival, list) and arrival:
        for index, item in enumerate(arrival):
            steps.append(_step(f"arrival_{index}", f"Step {index + 1}", item))
    return steps


_STEP_BUILDERS = {
    "stage_1": _steps_stage_1,
    "stage_1_5": _steps_stage_1_5,
    "stage_1_6": _steps_stage_1_6,
    "stage_2": _algorithm_steps,
    "stage_2_1": _algorithm_steps,
    "stage_3": _steps_stage_3,
    "stage_3_1": _steps_stage_3_1,
    "stage_3_2": _steps_stage_3_2,
    "stage_3_3": _steps_stage_3_3,
    "stage_3_4": _steps_stage_3_4,
    "stage_4": _steps_stage_4,
}


def _stage_status(output: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    if output.get("needs_clarification") is True:
        return "needs_clarification"
    if output.get("algorithm_status") == "needs_clarification":
        return "needs_clarification"
    if output.get("metric_relationship_status") == "needs_clarification":
        return "needs_clarification"
    if output.get("metric_completeness_status") == "needs_clarification":
        return "needs_clarification"
    mapped = _mapped_schema(output)
    if mapped.get("needs_clarification") is True:
        return "needs_clarification"
    if any(step.get("status") == "error" for step in steps):
        return "error"
    if any(step.get("status") == "warning" for step in steps):
        return "needs_clarification"
    return "completed"


def present_pipeline_stage(stage_name: str, output: dict[str, Any], *, phase: str = "completed") -> dict[str, Any]:
    catalog = stage_catalog_entry(stage_name)
    base = base_stage_id(stage_name)
    builder = _STEP_BUILDERS.get(base)
    steps = builder(output) if builder else [_step("output", "Stage output", output)]
    status = "running" if phase == "running" else _stage_status(output, steps)
    return {
        "id": stage_name,
        "base_id": base,
        "order": catalog.get("order", 99),
        "title": catalog.get("title", stage_name),
        "subtitle": catalog.get("subtitle", ""),
        "icon": catalog.get("icon", "⚙️"),
        "status": status,
        "phase": phase,
        "steps": steps,
        "output_keys": catalog.get("output_keys", []),
        "raw_output": output,
    }


def present_stage_started(stage_name: str) -> dict[str, Any]:
    catalog = stage_catalog_entry(stage_name)
    return {
        "id": stage_name,
        "base_id": base_stage_id(stage_name),
        "order": catalog.get("order", 99),
        "title": catalog.get("title", stage_name),
        "subtitle": catalog.get("subtitle", ""),
        "icon": catalog.get("icon", "⚙️"),
        "status": "running",
        "phase": "running",
        "steps": [],
        "output_keys": catalog.get("output_keys", []),
        "raw_output": None,
    }
