import json
from typing import Any

from langgraph.prebuilt import create_react_agent

from agents.intent_schema_agent.prompts import build_intent_schema_prompt
from agents.shared.parsers import (
    extract_json_object_from_react_result,
    last_agent_text,
)
from agents.shared.schema_helpers import normalize_semantic_schema_shape
from agents.shared.usage import normalize_usage_metadata
from api.schemas.ui_creation.generation import PlannerSchemaOut


def run_intent_schema_agent(
    llm,
    user_query: str,
    widget: str | None = None,
    apply_schema_overrides=None,
) -> tuple[dict, str, int, dict[str, Any] | None]:
    system_prompt = build_intent_schema_prompt(user_query, widget)
    react_agent = create_react_agent(llm, tools=[], prompt=system_prompt)
    result = react_agent.invoke({"messages": []})

    try:
        raw_schema_dict = extract_json_object_from_react_result(result, {"type", "title"})
        raw_schema_dict = normalize_semantic_schema_shape(raw_schema_dict)
        semantic_schema = PlannerSchemaOut(**raw_schema_dict)
    except Exception as exc:
        repair_message = f"""
Your previous response did not satisfy the required PlannerSchemaOut JSON contract.

Original User Request:
{user_query}

Selected Widget:
{widget if widget else "NULL"}

Previous Response:
{last_agent_text(result)}

Validation Issue:
{str(exc)[:1500]}

Recheck your output and return ONLY corrected valid JSON.
"""
        result = react_agent.invoke({"messages": [("user", repair_message)]})
        raw_schema_dict = extract_json_object_from_react_result(result, {"type", "title"})
        raw_schema_dict = normalize_semantic_schema_shape(raw_schema_dict)
        semantic_schema = PlannerSchemaOut(**raw_schema_dict)

    semantic_schema_dict = semantic_schema.model_dump()
    if apply_schema_overrides is not None:
        semantic_schema_dict = apply_schema_overrides(
            user_query,
            semantic_schema_dict,
            selected_widget=widget,
        )

    semantic_schema_json = json.dumps(semantic_schema_dict, indent=2, default=str)
    component_count = len(semantic_schema_dict.get("components", []))

    usage = None
    for msg in reversed(result.get("messages", [])):
        usage = normalize_usage_metadata(msg)
        if usage:
            break

    print("\n" + "=" * 80)
    print("AGENT 1 OUTPUT - SEMANTIC SCHEMA")
    print("=" * 80)
    print(semantic_schema_json)
    print("=" * 80 + "\n")

    return semantic_schema_dict, semantic_schema_json, component_count, usage
