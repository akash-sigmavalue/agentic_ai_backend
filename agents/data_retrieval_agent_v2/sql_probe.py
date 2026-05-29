import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import Settings


class SqlProbe(Protocol):
    def execute_approved_sql(self, sql_review_output: dict[str, Any], iteration: int) -> dict[str, Any]: ...


class SqlProbeService:
    """Executes reviewed read-only SQL and gathers evidence for the Observe stage."""

    _FORBIDDEN_SQL = re.compile(
        r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE|GRANT|REVOKE|CALL|EXECUTE)\b",
        re.IGNORECASE,
    )
    _FILTER_TEXT_COMPARE = re.compile(
        r"\b(?:(?:[A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*(=|ILIKE)\s*'((?:''|[^'])*)'",
        re.IGNORECASE,
    )
    _ZERO_ROW_CANDIDATE_COLUMNS = {
        "location_name": ("micro_market", "village_name"),
        "micro_market": ("location_name", "village_name"),
        "village_name": ("location_name", "micro_market"),
        "property_type": ("property_type_raw", "project_type"),
        "project_type": ("property_type",),
        "unit_configuration": ("unit_configuration_raw",),
    }

    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self._database_url = settings.transaction_database_url
        self._sample_limit = max(1, settings.sql_probe_sample_limit)
        self._output_limit = max(1, settings.sql_probe_output_limit)
        self._engine = engine

    def execute_approved_sql(
        self, sql_review_output: dict[str, Any], iteration: int
    ) -> dict[str, Any]:
        metric_relationship = sql_review_output.get("metric_relationship", "combined")
        output = self._base_output(metric_relationship, iteration)
        if self._engine is None and not self._database_url:
            output["non_execution_reason"] = (
                "DATABASE_URL is required to execute approved SQL in Stage 3.2."
            )
            output["final_probe_summary"] = output["non_execution_reason"]
            return output

        sql_items = self._approved_sql_items(sql_review_output, metric_relationship)
        if not sql_items:
            output["non_execution_reason"] = "SQL Review did not provide approved SQL to execute."
            output["final_probe_summary"] = output["non_execution_reason"]
            return output

        engine = self._engine or create_engine(self._database_url or "")
        all_success_with_data = True
        for sql_type, metric_name, query in sql_items:
            result = self._execute_one(engine, query)
            if sql_type == "combined":
                result["applicable"] = True
                output["execution_results"]["combined_sql"] = result
            else:
                result["metric_name"] = metric_name
                output["execution_results"]["individual_sql_queries"].append(result)

            if result["execution_status"] != "success" or result["row_count"] == 0:
                all_success_with_data = False
            if result["execution_status"] == "failed":
                output["correction_instructions_for_sql_observe"].append(
                    {
                        "issue_type": "execution_error",
                        "sql_type": sql_type,
                        "metric_name": metric_name,
                        "filter_name": "",
                        "original_mapped_column": "",
                        "mapped_value": "",
                        "suggested_candidate_column": "",
                        "evidence_count": 0,
                        "instruction": result["error_details"],
                    }
                )
                output["needs_correction"] = True
            elif result["row_count"] == 0:
                verifications = self._verify_zero_row_filters(engine, query, sql_type, metric_name)
                output["column_value_verification"].extend(verifications)
                for verification in verifications:
                    matches = verification["matched_candidate_columns"]
                    if not matches:
                        continue
                    best_match = max(matches, key=lambda item: item["match_count"])
                    output["correction_instructions_for_sql_observe"].append(
                        {
                            "issue_type": "wrong_column_mapping",
                            "sql_type": sql_type,
                            "metric_name": metric_name,
                            "filter_name": verification["filter_name"],
                            "original_mapped_column": verification["mapped_column"],
                            "mapped_value": verification["mapped_value"],
                            "suggested_candidate_column": best_match["candidate_column"],
                            "evidence_count": best_match["match_count"],
                            "instruction": (
                                f"Replace {verification['mapped_column']} with "
                                f"{best_match['candidate_column']} for value "
                                f"{verification['mapped_value']!r}."
                            ),
                        }
                    )
                    output["needs_correction"] = True

        output["sql_probe_status"] = "success" if all_success_with_data else "failed"
        output["send_to_sql_observe"] = True
        output["final_probe_summary"] = (
            "Approved SQL executed successfully and returned data."
            if all_success_with_data
            else "Probe requires observation because execution failed or returned no data."
        )
        return output

    def _execute_one(self, engine: Engine, query: str) -> dict[str, Any]:
        result = {
            "executed_sql": query,
            "execution_status": "not_executed",
            "row_count": 0,
            "output_availability": "not_executed",
            "sample_output": [],
            "table_output": [],
            "table_truncated": False,
            "output_summary": "",
            "error_details": "",
        }
        validation_error = self._read_only_validation_error(query)
        if validation_error:
            result["execution_status"] = "failed"
            result["error_details"] = validation_error
            return result
        try:
            countable_query = query.strip().rstrip(";")
            with engine.connect() as connection:
                row_count = int(
                    connection.execute(
                        text(f"SELECT COUNT(*) FROM ({countable_query}) AS sql_probe_result")
                    ).scalar_one()
                )
                output_rows = connection.execute(text(query)).mappings().fetchmany(self._output_limit)
            safe_rows = [self._json_safe_row(dict(row)) for row in output_rows]
            sample_limit = min(self._sample_limit, self._output_limit)
            sample_truncated = row_count > sample_limit
            table_truncated = row_count > self._output_limit
            result["execution_status"] = "success"
            result["row_count"] = row_count
            result["output_availability"] = "data_available" if row_count else "no_data"
            result["sample_output"] = safe_rows[:sample_limit]
            result["table_output"] = safe_rows
            result["table_truncated"] = table_truncated
            result["output_summary"] = (
                f"Returned {row_count} rows; table output is limited to {self._output_limit}; "
                f"sample is limited to {sample_limit}."
                if table_truncated
                else (
                    f"Returned {row_count} rows; table output includes all rows; "
                    f"sample is limited to {sample_limit}."
                    if sample_truncated
                    else f"Returned {row_count} row(s); table output includes all rows."
                )
            )
        except Exception as exc:
            result["execution_status"] = "failed"
            result["error_details"] = str(exc)
        return result

    def _verify_zero_row_filters(
        self, engine: Engine, query: str, sql_type: str, metric_name: str
    ) -> list[dict[str, Any]]:
        verifications: list[dict[str, Any]] = []
        for match in self._FILTER_TEXT_COMPARE.finditer(query):
            mapped_column, operator, escaped_value = match.groups()
            mapped_column = mapped_column.lower()
            candidates = self._ZERO_ROW_CANDIDATE_COLUMNS.get(mapped_column, ())
            if not candidates:
                continue
            mapped_value = escaped_value.replace("''", "'")
            if operator.upper() == "ILIKE":
                mapped_value = mapped_value.strip("%")
            candidate_matches: list[dict[str, Any]] = []
            for candidate in candidates:
                comparison_sql = (
                    f"LOWER(CAST({candidate} AS TEXT)) LIKE LOWER(:value)"
                    if operator.upper() == "ILIKE"
                    else f"{candidate} = :value"
                )
                params = {
                    "value": f"%{mapped_value}%" if operator.upper() == "ILIKE" else mapped_value
                }
                with engine.connect() as connection:
                    count = connection.execute(
                        text(
                            "SELECT COUNT(*) AS match_count FROM transactions "
                            f"WHERE {comparison_sql}"
                        ),
                        params,
                    ).scalar_one()
                if count:
                    candidate_matches.append(
                        {"candidate_column": candidate, "match_count": int(count)}
                    )
            verifications.append(
                {
                    "sql_type": sql_type,
                    "metric_name": metric_name,
                    "filter_name": mapped_column,
                    "mapped_column": mapped_column,
                    "mapped_value": mapped_value,
                    "value_found_in_mapped_column": False,
                    "mapped_column_match_count": 0,
                    "candidate_columns_checked": list(candidates),
                    "matched_candidate_columns": candidate_matches,
                    "verification_status": (
                        "found_in_other_column" if candidate_matches else "not_found"
                    ),
                }
            )
        return verifications

    @classmethod
    def _read_only_validation_error(cls, query: str) -> str:
        without_comments = re.sub(r"--.*?$|/\*.*?\*/", "", query, flags=re.MULTILINE | re.DOTALL)
        without_literals = re.sub(r"'(?:''|[^'])*'", "''", without_comments)
        stripped = without_literals.strip()
        if not re.match(r"^(?:SELECT|WITH)\b", stripped, flags=re.IGNORECASE):
            return "SQL Probe executes SELECT queries only."
        if ";" in stripped.rstrip(";"):
            return "SQL Probe does not allow multiple SQL statements."
        if cls._FORBIDDEN_SQL.search(stripped):
            return "SQL Probe rejected a non-read-only SQL operation."
        if re.search(r"\bSELECT\s+\*", stripped, flags=re.IGNORECASE):
            return "SQL Probe does not allow SELECT *."
        return ""

    @staticmethod
    def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
        return {key: SqlProbeService._json_safe_value(value) for key, value in row.items()}

    @staticmethod
    def _json_safe_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (date, datetime, time)):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: SqlProbeService._json_safe_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [SqlProbeService._json_safe_value(item) for item in value]
        return value

    @staticmethod
    def _approved_sql_items(
        sql_review_output: dict[str, Any], metric_relationship: str
    ) -> list[tuple[str, str, str]]:
        approved = sql_review_output.get("approved_sql") or {}
        if metric_relationship == "individual":
            return [
                ("individual", item.get("metric_name", ""), item.get("sql_query", ""))
                for item in approved.get("individual_sql_queries", [])
                if item.get("sql_query")
            ]
        combined = approved.get("combined_sql") or {}
        query = combined.get("sql_query", "")
        return [("combined", "", query)] if query else []

    @staticmethod
    def _base_output(metric_relationship: str, iteration: int) -> dict[str, Any]:
        return {
            "stage": "Stage 3.2 - SQL Probe",
            "sql_probe_status": "failed",
            "database": "transaction_db",
            "metric_relationship": metric_relationship,
            "execution_results": {
                "combined_sql": {
                    "applicable": metric_relationship == "combined",
                    "executed_sql": "",
                    "execution_status": "not_executed",
                    "row_count": 0,
                    "output_availability": "not_executed",
                    "sample_output": [],
                    "table_output": [],
                    "table_truncated": False,
                    "output_summary": "",
                    "error_details": "",
                },
                "individual_sql_queries": [],
            },
            "column_value_verification": [],
            "correction_instructions_for_sql_observe": [],
            "needs_correction": False,
            "send_to_sql_observe": False,
            "non_execution_reason": "",
            "final_probe_summary": "",
            "react_loop": {
                "iteration": iteration,
                "next_stage": "Stage 3.3 SQL Observe",
                "loop_status": "continue",
            },
        }
