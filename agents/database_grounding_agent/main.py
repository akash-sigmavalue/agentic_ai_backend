from __future__ import annotations

import json
from typing import Any

from langgraph.prebuilt import create_react_agent

from agents.database_grounding_agent.prompts import build_database_grounding_prompt
from agents.shared.parsers import extract_json_object_from_react_result
from agents.shared.schema_helpers import (
    ground_schema_from_mapping,
    normalize_resolver_shape,
)
from agents.shared.usage import normalize_usage_metadata
from api.schemas.ui_creation.generation import DBResolverPlanOut


def run_database_grounding_agent(
    llm,
    user_query: str,
    semantic_schema_dict: dict,
    semantic_schema_json: str,
    allowed_columns: list[str],
) -> tuple[dict, dict[str, Any] | None]:
    prompt = build_database_grounding_prompt(
        allowed_columns=allowed_columns,
        requested_type=semantic_schema_dict.get("type"),
    )
    react_agent = create_react_agent(llm, tools=[], prompt=prompt)
    user_message = f"""
Original User Query:
{user_query}

Semantic UI Schema from Agent 1:
{semantic_schema_json}

Resolve the semantic schema to the best real DB columns and generate grounded_schema, semantic_mapping, and sql_queries.
"""
    result = react_agent.invoke({"messages": [("user", user_message)]})
    resolver_dict = extract_json_object_from_react_result(
        result,
        {"can_answer", "grounded_schema", "semantic_mapping", "sql_queries"},
    )
    resolver_dict = normalize_resolver_shape(resolver_dict)
    resolver_dict = DBResolverPlanOut(**resolver_dict).model_dump()

    if (
        resolver_dict.get("can_answer")
        and isinstance(resolver_dict.get("grounded_schema"), dict)
        and "components" not in resolver_dict["grounded_schema"]
        and isinstance(resolver_dict.get("semantic_mapping"), dict)
    ):
        resolver_dict["grounded_schema"] = ground_schema_from_mapping(
            semantic_schema_dict,
            resolver_dict["semantic_mapping"],
        )

    usage = None
    for msg in reversed(result.get("messages", [])):
        usage = normalize_usage_metadata(msg)
        if usage:
            break

    print("\n" + "=" * 80)
    print("AGENT 2 OUTPUT - GROUNDED SCHEMA + SQL")
    print("=" * 80)
    print(json.dumps(resolver_dict, indent=2, default=str))
    print("=" * 80 + "\n")

    return resolver_dict, usage
