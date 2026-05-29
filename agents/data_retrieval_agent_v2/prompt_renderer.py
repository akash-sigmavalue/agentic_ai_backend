import json
from typing import Any

from . import promt
from .schema import SPACE_SCHEMA, TRANSACTION_QUERY_SCHEMA

from .semantic_defaults import DEFAULT_ATTRIBUTE_MASTER_TABLES, DEFAULT_DISTINCT_DATABASE_VALUES


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True)


def _merge_column_values(
    default_values: dict[str, Any], request_values: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(default_values)
    for column_name, value in request_values.items():
        if (
            isinstance(merged.get(column_name), list)
            and isinstance(value, list)
        ):
            merged[column_name] = list(dict.fromkeys([*merged[column_name], *value]))
        elif (
            isinstance(merged.get(column_name), dict)
            and isinstance(value, dict)
            and isinstance(merged[column_name].get("allowed_values"), list)
            and isinstance(value.get("allowed_values"), list)
        ):
            merged[column_name] = {
                **merged[column_name],
                **value,
                "allowed_values": list(
                    dict.fromkeys(
                        [*merged[column_name]["allowed_values"], *value["allowed_values"]]
                    )
                ),
            }
        else:
            merged[column_name] = value
    return merged


def _fill(template: str, replacements: dict[str, str]) -> str:
    """Replace only named context tokens; prompt JSON braces must remain literal."""
    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace("{" + token + "}", value)
    return rendered


class PromptRenderer:
    def stage_1(self, user_query: str) -> str:
        return _fill(
            promt.stage_1,
            {
                "schema": TRANSACTION_QUERY_SCHEMA,
                "space_schema": SPACE_SCHEMA,
                "user_query": user_query,
            },
        )

    def stage_1_5(self, user_query: str, stage_1_output: dict[str, Any]) -> str:
        return _fill(
            promt.stage_1_5,
            {
                "OUTPUT_JSON_SCHEMA": _json(stage_1_output["OUTPUT_JSON_SCHEMA"]),
                "MAPPED_JSON_SCHEMA": _json(stage_1_output["MAPPED_JSON_SCHEMA"]),
                "user_query": user_query,
            },
        )

    def stage_1_6(self, user_query: str, stage_1_5_output: dict[str, Any]) -> str:
        return _fill(
            promt.stage_1_6,
            {
                "MAPPED_JSON_SCHEMA": _json(stage_1_5_output["MAPPED_JSON_SCHEMA"]),
                "user_query": user_query,
            },
        )

    def stage_2(self, stage_1_6_output: dict[str, Any]) -> str:
        return _fill(
            promt.stage_2,
            {
                "schema": TRANSACTION_QUERY_SCHEMA,
                # The full Stage 1.6 payload includes its metric_relationship decision.
                "MAPPED_JSON_SCHEMA": _json(stage_1_6_output),
            },
        )

    def stage_2_1_prompt(self) -> str:
        return promt.stage_2_1

    def stage_2_1_context(
        self, stage_2_output: dict[str, Any], semantic_context: dict[str, Any]
    ) -> str:
        attribute_master_tables = _merge_column_values(
            DEFAULT_ATTRIBUTE_MASTER_TABLES,
            semantic_context.get("attribute_master_tables", {}),
        )
        distinct_database_values = _merge_column_values(
            DEFAULT_DISTINCT_DATABASE_VALUES,
            semantic_context.get("distinct_database_values", {}),
        )
        return _json(
            {
                "Stage 2 JSON schema": stage_2_output,
                "Transaction schema": TRANSACTION_QUERY_SCHEMA,
                "Attribute master tables": attribute_master_tables,
                "Distinct database values": distinct_database_values,
                "Lookup results": semantic_context.get("lookup_results", {}),
            }
        )

    def stage_3(self, stage_2_1_output: dict[str, Any]) -> str:
        return _fill(
            promt.stage_3,
            {
                "algorithm_status": str(stage_2_1_output.get("algorithm_status", "")),
                "final_algorithm": _json(stage_2_1_output),
                "final_algorithm_structure": _json(
                    stage_2_1_output.get("final_algorithm_structure", {})
                ),
                "schema": TRANSACTION_QUERY_SCHEMA,
            },
        )

    def stage_3_1(
        self,
        stage_2_1_output: dict[str, Any],
        sql_candidate_output: dict[str, Any],
        iteration: int = 1,
        max_iterations: int = 3,
    ) -> str:
        return _fill(
            promt.stage_3_1,
            {
                "final_algorithm_structure": _json(
                    stage_2_1_output.get("final_algorithm_structure", {})
                ),
                "sql_build_output": _json(sql_candidate_output),
                "react_iteration": str(iteration),
                "max_iterations": str(max_iterations),
                "schema": TRANSACTION_QUERY_SCHEMA,
            },
        )

    def stage_3_2(self, sql_review_output: dict[str, Any], iteration: int = 1) -> str:
        return _fill(
            promt.stage_3_2,
            {
                "sql_review_output": _json(sql_review_output),
                "react_iteration": str(iteration),
                "schema": TRANSACTION_QUERY_SCHEMA,
            },
        )

    def stage_3_3(
        self,
        stage_2_1_output: dict[str, Any],
        sql_review_output: dict[str, Any],
        sql_probe_output: dict[str, Any],
        iteration: int,
        max_iterations: int,
    ) -> str:
        return _fill(
            promt.stage_3_3,
            {
                "final_algorithm_structure": _json(
                    stage_2_1_output.get("final_algorithm_structure", {})
                ),
                "sql_review_output": _json(sql_review_output),
                "sql_probe_output": _json(sql_probe_output),
                "react_iteration": str(iteration),
                "max_iterations": str(max_iterations),
                "schema": TRANSACTION_QUERY_SCHEMA,
            },
        )

    def stage_3_4(
        self,
        stage_2_1_output: dict[str, Any],
        sql_review_output: dict[str, Any],
        sql_observe_output: dict[str, Any],
        iteration: int,
        max_iterations: int,
    ) -> str:
        return _fill(
            promt.stage_3_4,
            {
                "final_algorithm_structure": _json(
                    stage_2_1_output.get("final_algorithm_structure", {})
                ),
                "sql_review_output": _json(sql_review_output),
                "original_approved_sql": _json(sql_review_output.get("approved_sql", {})),
                "sql_observe_output": _json(sql_observe_output),
                "react_iteration": str(iteration),
                "next_react_iteration": str(iteration + 1),
                "max_iterations": str(max_iterations),
                "schema": TRANSACTION_QUERY_SCHEMA,
            },
        )

    def stage_4(
        self,
        user_query: str,
        stage_2_1_output: dict[str, Any],
        sql_review_output: dict[str, Any],
        sql_probe_output: dict[str, Any],
        sql_observe_output: dict[str, Any],
    ) -> str:
        return _fill(
            promt.stage_4,
            {
                "user_query": user_query,
                "final_algorithm_structure": _json(
                    stage_2_1_output.get("final_algorithm_structure", {})
                ),
                "sql_review_output": _json(sql_review_output),
                "sql_probe_output": _json(sql_probe_output),
                "sql_observe_output": _json(sql_observe_output),
            },
        )
