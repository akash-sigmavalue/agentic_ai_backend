from __future__ import annotations

import json
from typing import Any, Generator

from core.llm import get_llm
from database.ui_creation.sql_executor import execute_sql_queries as _execute_sql_queries
from agents.database_grounding_agent.main import run_database_grounding_agent
from agents.intent_schema_agent.main import run_intent_schema_agent
from agents.ui_generation_agent.main import run_ui_generation_agent
from agents.uploaded_data_grounding_agent.main import run_uploaded_data_grounding_agent
from orchestration.ui_creation.events import make_event as _make_event
from orchestration.ui_creation.events import make_sub_event as _make_sub_event
from orchestration.ui_creation.pending_state import pop_pending_intent_state as _pop_pending_intent_state
from orchestration.ui_creation.pending_state import store_pending_intent_state as _store_pending_intent_state
from utils.ui_creation.output_packaging import build_final_result as _build_final_result
from utils.ui_creation.output_packaging import build_unanswerable_final_result as _build_unanswerable_final_result
from utils.ui_creation.schema_overrides import apply_schema_overrides as _apply_schema_overrides

ALLOWED_COLUMNS = [
    "id",
    "name",
    "department",
    "performance_score",
    "attendance_pct",
    "attrition_score",
    "salary",
    "employee_code",
    "gender",
    "date_of_birth",
    "email",
    "mobile_number",
    "designation",
    "joining_date",
    "employment_status",
    "leave_balance",
]

def _continue_analysis_pipeline_stream(
    llm,
    user_query: str,
    widget: str | None,
    semantic_schema_dict: dict,
    semantic_schema_json: str,
    component_count: int,
    planner_usage: dict[str, Any] | None,
    uploaded_table_name: str | None = None,
) -> Generator[dict, None, None]:
    resolver_node = "file_data_agent" if uploaded_table_name else "planning_agent"
    intent_to_resolver_edge = (
        "intent_to_file_data_agent" if uploaded_table_name else "intent_to_planning_agent"
    )
    resolver_to_db_edge = "file_data_agent_to_db" if uploaded_table_name else "planning_to_db"
    resolver_label = "File Data Agent" if uploaded_table_name else "Planning Agent"

    yield _make_event("edge_start", intent_to_resolver_edge, f"Connecting to {resolver_label}")
    planning_message = (
        "File Data Agent is grounding schema and generating SQL"
        if uploaded_table_name
        else "Planning Agent is grounding schema and generating SQL"
    )
    yield _make_event("stage_start", resolver_node, planning_message)

    yield _make_sub_event(
        "substage_start",
        resolver_node,
        "schema_grounding",
        "Grounding schema to uploaded file fields" if uploaded_table_name else "Grounding schema to database fields",
    )

    if uploaded_table_name:
        resolver_result, resolver_usage = run_uploaded_data_grounding_agent(
            llm,
            user_query,
            semantic_schema_dict,
            semantic_schema_json,
            uploaded_table_name,
        )
    else:
        resolver_result, resolver_usage = run_database_grounding_agent(
            llm, user_query, semantic_schema_dict, semantic_schema_json, ALLOWED_COLUMNS
        )

    can_answer = resolver_result.get("can_answer", True)
    if not can_answer:
        reason = resolver_result.get("reason", "Agent 2 could not ground the schema.")
        final_result = _build_unanswerable_final_result(
            user_query=user_query,
            reason=reason,
            semantic_schema_dict=semantic_schema_dict,
            resolver_result=resolver_result,
        )
        final_result["usage"] = {
            "intent_agent": planner_usage,
            "planning_agent": resolver_usage,
            "ui_agent": None,
            "output": None,
        }
        yield _make_event(
            "stage_complete",
            resolver_node,
            f"{resolver_label} could not answer from available data",
            {
                "preview": {
                    "title": "No matching data source",
                    "reason": reason,
                },
                "grounded_schema": resolver_result.get("grounded_schema", {}),
                "semantic_mapping": resolver_result.get("semantic_mapping", {}),
                "sql_queries": resolver_result.get("sql_queries", []),
                "usage": resolver_usage,
            },
        )
        yield _make_event("edge_complete", intent_to_resolver_edge, f"{resolver_label} connection finished")
        yield _make_event("final_result", "output", "Response ready", final_result)
        return

    grounded_schema_dict = resolver_result.get("grounded_schema", {})
    semantic_mapping = resolver_result.get("semantic_mapping", {})
    sql_queries = resolver_result.get("sql_queries", [])
    grounded_schema_json = json.dumps(grounded_schema_dict, indent=2, default=str)

    mapping_items = list(semantic_mapping.items()) if isinstance(semantic_mapping, dict) else []
    mapping_preview = mapping_items[:3]

    yield _make_sub_event(
        "substage_complete",
        resolver_node,
        "schema_grounding",
        "Fields mapped",
        {
            "mapping_preview": mapping_preview,
        },
    )

    yield _make_sub_event(
        "substage_start",
        resolver_node,
        "sql_generation",
        "Generating SQL query",
    )

    yield _make_sub_event(
        "substage_complete",
        resolver_node,
        "sql_generation",
        "SQL ready",
        {
            "sql": sql_queries[0] if sql_queries else "",
            "query_count": len(sql_queries),
        },
    )

    yield _make_event(
        "stage_complete",
        resolver_node,
        f"{resolver_label} completed",
        {
            "preview": {
                "title": "Retrieval Plan",
                "mapping_count": len(mapping_items),
                "top_mappings": mapping_preview,
                "query_count": len(sql_queries),
                "sql_preview": (
                    sql_queries[0][:160] + "..."
                    if sql_queries and len(sql_queries[0]) > 160
                    else (sql_queries[0] if sql_queries else "")
                ),
            },
            "grounded_schema": grounded_schema_dict,
            "semantic_mapping": semantic_mapping,
            "sql_queries": sql_queries,
            "usage": resolver_usage,
        },
    )
    yield _make_event("edge_complete", intent_to_resolver_edge, f"{resolver_label} connection finished")

    yield _make_event("edge_start", resolver_to_db_edge, "Connecting to database")
    yield _make_event("stage_start", "database", "Fetching real data from database")

    yield _make_sub_event(
        "substage_start",
        "database",
        "database_execution",
        "Executing SQL",
    )

    if sql_queries:
        real_data_dict = _execute_sql_queries(sql_queries)
        real_data_json = json.dumps(real_data_dict, indent=2, default=str)
        dataset_0 = real_data_dict.get("dataset_0", [])
        row_count = len(dataset_0)
        sample_row = dataset_0[0] if dataset_0 else {}
    else:
        real_data_dict = {}
        real_data_json = "[]"
        row_count = 0
        sample_row = {}

    yield _make_sub_event(
        "substage_complete",
        "database",
        "database_execution",
        "Data fetched",
        {
            "row_count": row_count,
            "sample_row": sample_row,
        },
    )

    yield _make_event(
        "stage_complete",
        "database",
        "Database fetch completed",
        {
            "preview": {
                "title": "Fetched Data",
                "row_count": row_count,
                "sample_row": sample_row,
            },
            "row_count": row_count,
            "sample_row": sample_row,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "model_name": "database",
                "model_provider": "system",
            },
        },
    )
    yield _make_event("edge_complete", resolver_to_db_edge, "Database connection finished")

    yield _make_event("edge_start", "db_to_ui_agent", "Passing real data to UI Agent")
    yield _make_event("stage_start", "ui_agent", "UI Agent is composing final UI")

    yield _make_sub_event(
        "substage_start",
        "ui_agent",
        "ui_decision",
        "Selecting response format",
    )

    ui_type = grounded_schema_dict.get("type") or semantic_schema_dict.get("type") or "component"

    yield _make_sub_event(
        "substage_complete",
        "ui_agent",
        "ui_decision",
        "UI structure decided",
        {
            "ui_type": ui_type,
            "component_count": component_count,
        },
    )

    yield _make_sub_event(
        "substage_start",
        "ui_agent",
        "ui_generation",
        "Building JSX response",
    )

    jsx_output, ui_usage = run_ui_generation_agent(
        llm=llm,
        user_query=user_query,
        grounded_schema_json=grounded_schema_json,
        component_count=component_count,
        real_data_json=real_data_json,
    )

    jsx_preview = jsx_output[:400] + ("..." if len(jsx_output) > 400 else "")

    yield _make_sub_event(
        "substage_complete",
        "ui_agent",
        "ui_generation",
        "UI ready",
        {
            "jsx_preview": jsx_preview,
        },
    )

    yield _make_event(
        "stage_complete",
        "ui_agent",
        "UI Agent completed",
        {
            "preview": {
                "title": "Generated UI",
                "ui_type": ui_type,
                "component_count": component_count,
                "status": "JSX generated",
            },
            "usage": ui_usage,
        },
    )
    yield _make_event("edge_complete", "db_to_ui_agent", "UI Agent connection finished")

    yield _make_event("edge_start", "ui_agent_to_output", "Preparing final output")
    yield _make_event("stage_start", "output", "Finalizing JSX response")

    yield _make_sub_event(
        "substage_start",
        "output",
        "output_packaging",
        "Packaging final response",
    )

    final_result = _build_final_result(
        jsx_output=jsx_output,
        semantic_schema_dict=semantic_schema_dict,
        grounded_schema_dict=grounded_schema_dict,
        semantic_mapping=semantic_mapping,
        real_data_json=real_data_json,
    )

    final_result["usage"] = {
        "intent_agent": planner_usage,
        "planning_agent": resolver_usage,
        "ui_agent": ui_usage,
        "output": ui_usage,
    }

    if row_count > 0 and isinstance(sample_row, dict):
        final_preview = {
            "title": "Final Response",
            "values": list(sample_row.items())[:3],
        }
    else:
        final_preview = {
            "title": "Final Response",
            "values": [],
        }

    yield _make_sub_event(
        "substage_complete",
        "output",
        "output_packaging",
        "Response ready",
        final_preview,
    )

    yield _make_event(
        "stage_complete",
        "output",
        "Final output ready",
        {
            "preview": final_preview,
            "usage": ui_usage,
        },
    )
    yield _make_event("edge_complete", "ui_agent_to_output", "Output connection finished")

    yield _make_event(
        "final_result",
        "output",
        "Response ready",
        final_result,
    )


def execute_analysis_pipeline(
    user_query: str,
    widget: str | None = None,
    uploaded_table_name: str | None = None,
) -> dict:
    llm = get_llm()

    semantic_schema_dict, semantic_schema_json, component_count, planner_usage = run_intent_schema_agent(
        llm,
        user_query,
        widget,
        apply_schema_overrides=_apply_schema_overrides,
    )

    if uploaded_table_name:
        resolver_result, resolver_usage = run_uploaded_data_grounding_agent(
            llm, user_query, semantic_schema_dict, semantic_schema_json, uploaded_table_name
        )
    else:
        resolver_result, resolver_usage = run_database_grounding_agent(
            llm, user_query, semantic_schema_dict, semantic_schema_json, ALLOWED_COLUMNS
        )

    can_answer = resolver_result.get("can_answer", True)
    if not can_answer:
        reason = resolver_result.get("reason", "Agent 2 could not ground the schema.")
        final_result = _build_unanswerable_final_result(
            user_query=user_query,
            reason=reason,
            semantic_schema_dict=semantic_schema_dict,
            resolver_result=resolver_result,
        )
        final_result["usage"] = {
            "intent_agent": planner_usage,
            "planning_agent": resolver_usage,
            "ui_agent": None,
            "output": None,
        }
        return final_result

    grounded_schema_dict = resolver_result.get("grounded_schema", {})
    semantic_mapping = resolver_result.get("semantic_mapping", {})
    sql_queries = resolver_result.get("sql_queries", [])

    grounded_schema_json = json.dumps(grounded_schema_dict, indent=2, default=str)

    if sql_queries:
        real_data_dict = _execute_sql_queries(sql_queries)
        real_data_json = json.dumps(real_data_dict, indent=2, default=str)
    else:
        real_data_json = "[]"

    jsx_output, ui_usage = run_ui_generation_agent(
        llm=llm,
        user_query=user_query,
        grounded_schema_json=grounded_schema_json,
        component_count=component_count,
        real_data_json=real_data_json,
    )

    final_result = _build_final_result(
        jsx_output=jsx_output,
        semantic_schema_dict=semantic_schema_dict,
        grounded_schema_dict=grounded_schema_dict,
        semantic_mapping=semantic_mapping,
        real_data_json=real_data_json,
    )

    final_result["usage"] = {
        "intent_agent": planner_usage,
        "planning_agent": resolver_usage,
        "ui_agent": ui_usage,
        "output": ui_usage,
    }

    return final_result


def execute_analysis_pipeline_stream(
    user_query: str,
    widget: str | None = None,
    uploaded_table_name: str | None = None,
    pause_after_intent: bool = False,
) -> Generator[dict, None, None]:
    llm = get_llm()

    try:
        yield _make_event("stage_start", "query", "Query received")
        yield _make_event(
            "stage_complete",
            "query",
            "Query accepted",
            {
                "preview": {
                    "title": "User Query",
                    "summary": user_query,
                    "widget": widget,
                    "file_id": uploaded_table_name,
                }
            },
        )

        yield _make_event("edge_start", "query_to_intent_agent", "Connecting to Intent Agent")
        yield _make_event("stage_start", "intent_agent", "Intent Agent is generating semantic schema")

        yield _make_sub_event(
            "substage_start",
            "intent_agent",
            "intent_understanding",
            "Understanding query",
        )

        yield _make_sub_event(
            "substage_complete",
            "intent_agent",
            "intent_understanding",
            "Intent understood",
            {
                "summary": user_query,
                "widget": widget,
                "file_id": uploaded_table_name,
            },
        )

        yield _make_sub_event(
            "substage_start",
            "intent_agent",
            "semantic_schema_generation",
            "Building semantic schema",
        )

        semantic_schema_dict, semantic_schema_json, component_count, planner_usage = run_intent_schema_agent(
            llm,
            user_query,
            widget,
            apply_schema_overrides=_apply_schema_overrides,
        )

        semantic_components = semantic_schema_dict.get("components", [])
        first_component = semantic_components[0] if semantic_components else {}
        semantic_columns = first_component.get("columns", []) or semantic_schema_dict.get("columns", [])

        yield _make_sub_event(
            "substage_complete",
            "intent_agent",
            "semantic_schema_generation",
            "Schema ready",
            {
                "title": semantic_schema_dict.get("title"),
                "columns": semantic_columns,
                "component_count": component_count,
            },
        )

        yield _make_event(
            "stage_complete",
            "intent_agent",
            "Intent Agent completed",
            {
                "preview": {
                    "title": semantic_schema_dict.get("title") or "Semantic Schema",
                    "type": semantic_schema_dict.get("type"),
                    "fields": semantic_columns[:6],
                    "component_count": component_count,
                },
                "title": semantic_schema_dict.get("title"),
                "component_count": component_count,
                "semantic_schema_plan": semantic_schema_dict,
                "usage": planner_usage,
            },
        )
        yield _make_event("edge_complete", "query_to_intent_agent", "Intent Agent connection finished")

        if pause_after_intent:
            plan_id = _store_pending_intent_state(
                user_query=user_query,
                widget=widget,
                semantic_schema_dict=semantic_schema_dict,
                semantic_schema_json=semantic_schema_json,
                component_count=component_count,
                planner_usage=planner_usage,
            )
            yield _make_event(
                "awaiting_file_decision",
                "file_decision",
                "Intent schema is ready. Upload a file or continue without one.",
                {
                    "plan_id": plan_id,
                    "semantic_schema_plan": semantic_schema_dict,
                    "component_count": component_count,
                    "next_actions": {
                        "with_file": "POST /generation/stream/resume with plan_id and file",
                        "without_file": "POST /generation/stream/resume with plan_id only",
                    },
                },
            )
            return

        resolver_node = "file_data_agent" if uploaded_table_name else "planning_agent"
        intent_to_resolver_edge = (
            "intent_to_file_data_agent" if uploaded_table_name else "intent_to_planning_agent"
        )
        resolver_to_db_edge = "file_data_agent_to_db" if uploaded_table_name else "planning_to_db"
        resolver_label = "File Data Agent" if uploaded_table_name else "Planning Agent"

        yield _make_event("edge_start", intent_to_resolver_edge, f"Connecting to {resolver_label}")
        planning_message = (
            "File Data Agent is grounding schema and generating SQL"
            if uploaded_table_name
            else "Planning Agent is grounding schema and generating SQL"
        )
        yield _make_event("stage_start", resolver_node, planning_message)

        yield _make_sub_event(
            "substage_start",
            resolver_node,
            "schema_grounding",
            "Grounding schema to uploaded file fields" if uploaded_table_name else "Grounding schema to database fields",
        )

        if uploaded_table_name:
            resolver_result, resolver_usage = run_uploaded_data_grounding_agent(
                llm,
                user_query,
                semantic_schema_dict,
                semantic_schema_json,
                uploaded_table_name,
            )
        else:
            resolver_result, resolver_usage = run_database_grounding_agent(
                llm, user_query, semantic_schema_dict, semantic_schema_json, ALLOWED_COLUMNS
            )

        can_answer = resolver_result.get("can_answer", True)
        if not can_answer:
            reason = resolver_result.get("reason", "Agent 2 could not ground the schema.")
            final_result = _build_unanswerable_final_result(
                user_query=user_query,
                reason=reason,
                semantic_schema_dict=semantic_schema_dict,
                resolver_result=resolver_result,
            )
            final_result["usage"] = {
                "intent_agent": planner_usage,
                "planning_agent": resolver_usage,
                "ui_agent": None,
                "output": None,
            }
            yield _make_event(
                "stage_complete",
                resolver_node,
                f"{resolver_label} could not answer from available data",
                {
                    "preview": {
                        "title": "No matching data source",
                        "reason": reason,
                    },
                    "grounded_schema": resolver_result.get("grounded_schema", {}),
                    "semantic_mapping": resolver_result.get("semantic_mapping", {}),
                    "sql_queries": resolver_result.get("sql_queries", []),
                    "usage": resolver_usage,
                },
            )
            yield _make_event("edge_complete", intent_to_resolver_edge, f"{resolver_label} connection finished")
            yield _make_event("final_result", "output", "Response ready", final_result)
            return

        grounded_schema_dict = resolver_result.get("grounded_schema", {})
        semantic_mapping = resolver_result.get("semantic_mapping", {})
        sql_queries = resolver_result.get("sql_queries", [])
        grounded_schema_json = json.dumps(grounded_schema_dict, indent=2, default=str)

        mapping_items = list(semantic_mapping.items()) if isinstance(semantic_mapping, dict) else []
        mapping_preview = mapping_items[:3]

        yield _make_sub_event(
            "substage_complete",
            resolver_node,
            "schema_grounding",
            "Fields mapped",
            {
                "mapping_preview": mapping_preview,
            },
        )

        yield _make_sub_event(
            "substage_start",
            resolver_node,
            "sql_generation",
            "Generating SQL query",
        )

        yield _make_sub_event(
            "substage_complete",
            resolver_node,
            "sql_generation",
            "SQL ready",
            {
                "sql": sql_queries[0] if sql_queries else "",
                "query_count": len(sql_queries),
            },
        )

        yield _make_event(
            "stage_complete",
            resolver_node,
            f"{resolver_label} completed",
            {
                "preview": {
                    "title": "Retrieval Plan",
                    "mapping_count": len(mapping_items),
                    "top_mappings": mapping_preview,
                    "query_count": len(sql_queries),
                    "sql_preview": (
                        sql_queries[0][:160] + "..."
                        if sql_queries and len(sql_queries[0]) > 160
                        else (sql_queries[0] if sql_queries else "")
                    ),
                },
                "grounded_schema": grounded_schema_dict,
                "semantic_mapping": semantic_mapping,
                "sql_queries": sql_queries,
                "usage": resolver_usage,
            },
        )
        yield _make_event("edge_complete", intent_to_resolver_edge, f"{resolver_label} connection finished")

        yield _make_event("edge_start", resolver_to_db_edge, "Connecting to database")
        yield _make_event("stage_start", "database", "Fetching real data from database")

        yield _make_sub_event(
            "substage_start",
            "database",
            "database_execution",
            "Executing SQL",
        )

        if sql_queries:
            real_data_dict = _execute_sql_queries(sql_queries)
            real_data_json = json.dumps(real_data_dict, indent=2, default=str)
            dataset_0 = real_data_dict.get("dataset_0", [])
            row_count = len(dataset_0)
            sample_row = dataset_0[0] if dataset_0 else {}
        else:
            real_data_dict = {}
            real_data_json = "[]"
            row_count = 0
            sample_row = {}

        yield _make_sub_event(
            "substage_complete",
            "database",
            "database_execution",
            "Data fetched",
            {
                "row_count": row_count,
                "sample_row": sample_row,
            },
        )

        yield _make_event(
            "stage_complete",
            "database",
            "Database fetch completed",
            {
                "preview": {
                    "title": "Fetched Data",
                    "row_count": row_count,
                    "sample_row": sample_row,
                },
                "row_count": row_count,
                "sample_row": sample_row,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "model_name": "database",
                    "model_provider": "system",
                },
            },
        )
        yield _make_event("edge_complete", resolver_to_db_edge, "Database connection finished")

        yield _make_event("edge_start", "db_to_ui_agent", "Passing real data to UI Agent")
        yield _make_event("stage_start", "ui_agent", "UI Agent is composing final UI")

        yield _make_sub_event(
            "substage_start",
            "ui_agent",
            "ui_decision",
            "Selecting response format",
        )

        ui_type = grounded_schema_dict.get("type") or semantic_schema_dict.get("type") or "component"

        yield _make_sub_event(
            "substage_complete",
            "ui_agent",
            "ui_decision",
            "UI structure decided",
            {
                "ui_type": ui_type,
                "component_count": component_count,
            },
        )

        yield _make_sub_event(
            "substage_start",
            "ui_agent",
            "ui_generation",
            "Building JSX response",
        )

        jsx_output, ui_usage = run_ui_generation_agent(
            llm=llm,
            user_query=user_query,
            grounded_schema_json=grounded_schema_json,
            component_count=component_count,
            real_data_json=real_data_json,
        )

        jsx_preview = jsx_output[:400] + ("..." if len(jsx_output) > 400 else "")

        yield _make_sub_event(
            "substage_complete",
            "ui_agent",
            "ui_generation",
            "UI ready",
            {
                "jsx_preview": jsx_preview,
            },
        )

        yield _make_event(
            "stage_complete",
            "ui_agent",
            "UI Agent completed",
            {
                "preview": {
                    "title": "Generated UI",
                    "ui_type": ui_type,
                    "component_count": component_count,
                    "status": "JSX generated"
                },
                "usage": ui_usage,
            },
        )
        yield _make_event("edge_complete", "db_to_ui_agent", "UI Agent connection finished")

        yield _make_event("edge_start", "ui_agent_to_output", "Preparing final output")
        yield _make_event("stage_start", "output", "Finalizing JSX response")

        yield _make_sub_event(
            "substage_start",
            "output",
            "output_packaging",
            "Packaging final response",
        )

        final_result = _build_final_result(
            jsx_output=jsx_output,
            semantic_schema_dict=semantic_schema_dict,
            grounded_schema_dict=grounded_schema_dict,
            semantic_mapping=semantic_mapping,
            real_data_json=real_data_json,
        )

        final_result["usage"] = {
            "intent_agent": planner_usage,
            "planning_agent": resolver_usage,
            "ui_agent": ui_usage,
            "output": ui_usage,
        }

        final_preview = {}
        if row_count > 0 and isinstance(sample_row, dict):
            final_preview = {
                "title": "Final Response",
                "values": list(sample_row.items())[:3],
            }
        else:
            final_preview = {
                "title": "Final Response",
                "values": [],
            }

        yield _make_sub_event(
            "substage_complete",
            "output",
            "output_packaging",
            "Response ready",
            final_preview,
        )

        yield _make_event(
            "stage_complete",
            "output",
            "Final output ready",
            {
                "preview": final_preview,
                "usage": ui_usage,
            },
        )
        yield _make_event("edge_complete", "ui_agent_to_output", "Output connection finished")

        yield _make_event(
            "final_result",
            "output",
            "Response ready",
            final_result,
        )

    except Exception as e:
        yield _make_event(
            "error",
            "system",
            f"Pipeline failed: {str(e)}",
            {},
        )


def resume_analysis_pipeline_stream(
    plan_id: str,
    uploaded_table_name: str | None = None,
) -> Generator[dict, None, None]:
    llm = get_llm()

    try:
        state = _pop_pending_intent_state(plan_id)

        yield _make_event(
            "stage_complete",
            "file_decision",
            "File decision received",
            {
                "preview": {
                    "title": "File Decision",
                    "mode": "uploaded_file" if uploaded_table_name else "default_database",
                    "file_id": uploaded_table_name,
                }
            },
        )

        yield from _continue_analysis_pipeline_stream(
            llm=llm,
            user_query=state["user_query"],
            widget=state["widget"],
            semantic_schema_dict=state["semantic_schema_dict"],
            semantic_schema_json=state["semantic_schema_json"],
            component_count=state["component_count"],
            planner_usage=state["planner_usage"],
            uploaded_table_name=uploaded_table_name,
        )

    except Exception as e:
        yield _make_event(
            "error",
            "system",
            f"Pipeline failed: {str(e)}",
            {},
        )
