import json
import logging
from collections.abc import Callable
from typing import Any

from .clarification_ui import build_clarification_fields
from .llm import JsonAgent
from .models import PipelineResponse
from .prompt_renderer import PromptRenderer
from .sql_probe import SqlProbe

logger = logging.getLogger(__name__)

StageHook = Callable[[str, str, dict[str, Any] | None], None]


class SqlAgentWorkflow:
    """Executes the supplied prompt stages in their dependency order."""

    def __init__(
        self,
        agent: JsonAgent,
        renderer: PromptRenderer | None = None,
        sql_probe: SqlProbe | None = None,
        max_react_iterations: int = 3,
    ) -> None:
        self._agent = agent
        self._renderer = renderer or PromptRenderer()
        self._sql_probe = sql_probe
        self._max_react_iterations = max(1, max_react_iterations)

    @staticmethod
    def _emit_stage(stage_hook: StageHook | None, phase: str, stage_name: str, output: dict[str, Any] | None = None) -> None:
        if stage_hook is not None:
            stage_hook(phase, stage_name, output)

    async def run(
        self,
        user_query: str,
        include_intermediate_stages: bool = True,
        semantic_context: dict[str, Any] | None = None,
        stage_hook: StageHook | None = None,
    ) -> PipelineResponse:
        stages: dict[str, dict[str, Any]] = {}
        semantic_context = semantic_context or {}

        self._emit_stage(stage_hook, "started", "stage_1")
        stage_1 = await self._agent.complete_json("stage_1", self._renderer.stage_1(user_query))
        stages["stage_1"] = stage_1
        self._emit_stage(stage_hook, "completed", "stage_1", stage_1)
        logger.info("Stage 1 output:\n%s", json.dumps(stage_1, indent=2))
        if self._needs_clarification(stage_1):
            return self._clarification_response(
                user_query, "stage_1", stage_1, stages, include_intermediate_stages
            )
        self._require_keys("stage_1", stage_1, "OUTPUT_JSON_SCHEMA", "MAPPED_JSON_SCHEMA")

        self._emit_stage(stage_hook, "started", "stage_1_5")
        stage_1_5 = await self._agent.complete_json(
            "stage_1_5", self._renderer.stage_1_5(user_query, stage_1)
        )
        stages["stage_1_5"] = stage_1_5
        self._emit_stage(stage_hook, "completed", "stage_1_5", stage_1_5)
        logger.info("Stage 1.5 output:\n%s", json.dumps(stage_1_5, indent=2))
        if self._needs_clarification(stage_1_5):
            return self._clarification_response(
                user_query, "stage_1_5", stage_1_5, stages, include_intermediate_stages
            )
        self._require_keys("stage_1_5", stage_1_5, "MAPPED_JSON_SCHEMA")

        self._emit_stage(stage_hook, "started", "stage_1_6")
        stage_1_6 = await self._agent.complete_json(
            "stage_1_6", self._renderer.stage_1_6(user_query, stage_1_5)
        )
        stages["stage_1_6"] = stage_1_6
        self._emit_stage(stage_hook, "completed", "stage_1_6", stage_1_6)
        logger.info("Stage 1.6 output:\n%s", json.dumps(stage_1_6, indent=2))
        if self._needs_clarification(stage_1_6):
            return self._clarification_response(
                user_query, "stage_1_6", stage_1_6, stages, include_intermediate_stages
            )

        self._emit_stage(stage_hook, "started", "stage_2")
        stage_2 = await self._agent.complete_json("stage_2", self._renderer.stage_2(stage_1_6))
        stages["stage_2"] = stage_2
        self._emit_stage(stage_hook, "completed", "stage_2", stage_2)
        logger.info("Stage 2 output:\n%s", json.dumps(stage_2, indent=2))
        if self._needs_clarification(stage_2):
            return self._clarification_response(
                user_query, "stage_2", stage_2, stages, include_intermediate_stages
            )

        self._emit_stage(stage_hook, "started", "stage_2_1")
        stage_2_1 = await self._agent.complete_json(
            "stage_2_1",
            self._renderer.stage_2_1_prompt(),
            self._renderer.stage_2_1_context(stage_2, semantic_context),
        )
        stages["stage_2_1"] = stage_2_1
        self._emit_stage(stage_hook, "completed", "stage_2_1", stage_2_1)
        logger.info("Stage 2.1 output:\n%s", json.dumps(stage_2_1, indent=2))
        if self._needs_clarification(stage_2_1):
            return self._clarification_response(
                user_query, "stage_2_1", stage_2_1, stages, include_intermediate_stages
            )

        self._emit_stage(stage_hook, "started", "stage_3")
        stage_3 = await self._agent.complete_json("stage_3", self._renderer.stage_3(stage_2_1))
        stages["stage_3"] = stage_3
        self._emit_stage(stage_hook, "completed", "stage_3", stage_3)
        logger.info("Stage 3 generated SQL output:\n%s", json.dumps(stage_3, indent=2))
        if self._needs_clarification(stage_3):
            return self._clarification_response(
                user_query, "stage_3", stage_3, stages, include_intermediate_stages
            )
        if stage_3.get("sql_build_status") != "ready":
            return self._react_response(
                user_query,
                "blocked",
                "SQL Build did not produce SQL ready for review and execution.",
                "stage_3",
                "Inspect the SQL Build blocking reason.",
                stage_3,
                None,
                None,
                None,
                None,
                [],
                stages,
                include_intermediate_stages,
            )

        sql_candidate = stage_3
        react_iterations: list[dict[str, Any]] = []
        latest_review: dict[str, Any] | None = None
        latest_probe: dict[str, Any] | None = None
        latest_observe: dict[str, Any] | None = None
        latest_fix: dict[str, Any] | None = None
        latest_final: dict[str, Any] | None = None

        for iteration in range(1, self._max_react_iterations + 1):
            iteration_output: dict[str, Any] = {"iteration": iteration}
            review_stage_key = self._loop_stage_key("stage_3_1", iteration)
            self._emit_stage(stage_hook, "started", review_stage_key)
            latest_review = await self._agent.complete_json(
                "stage_3_1",
                self._renderer.stage_3_1(
                    stage_2_1,
                    sql_candidate,
                    iteration,
                    self._max_react_iterations,
                ),
            )
            self._store_loop_stage(stages, "stage_3_1", iteration, latest_review)
            self._emit_stage(stage_hook, "completed", review_stage_key, latest_review)
            iteration_output["sql_review_output"] = latest_review
            logger.info(
                "Stage 3.1 SQL review output, iteration %s:\n%s",
                iteration,
                json.dumps(latest_review, indent=2),
            )
            if self._needs_clarification(latest_review):
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "needs_clarification",
                    f"Clarification is required at stage 3.1 during ReAct iteration {iteration}.",
                    "stage_3_1",
                    (
                        "Submit one complete corrected query containing your original requirements "
                        "and the clarification answer; the pipeline will rerun from Stage 1."
                    ),
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                    self._clarification_question(latest_review),
                )
            if latest_review.get("sql_review_status") != "approved":
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "blocked",
                    "SQL Review did not approve SQL for database execution.",
                    "stage_3_1",
                    "Inspect SQL Review errors and fix instructions.",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )
            if self._sql_probe is None:
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "blocked",
                    "SQL Probe is not configured for this pipeline.",
                    "stage_3_2",
                    "Configure the transaction database connection for SQL Probe execution.",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )

            probe_stage_key = self._loop_stage_key("stage_3_2", iteration)
            self._emit_stage(stage_hook, "started", probe_stage_key)
            latest_probe = self._sql_probe.execute_approved_sql(latest_review, iteration)
            self._store_loop_stage(stages, "stage_3_2", iteration, latest_probe)
            self._emit_stage(stage_hook, "completed", probe_stage_key, latest_probe)
            iteration_output["sql_probe_output"] = latest_probe
            logger.info(
                "Stage 3.2 SQL probe output, iteration %s:\n%s",
                iteration,
                json.dumps(latest_probe, indent=2),
            )
            if latest_probe.get("non_execution_reason"):
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "blocked",
                    "Approved SQL could not be executed by SQL Probe.",
                    "stage_3_2",
                    latest_probe["non_execution_reason"],
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )

            observe_stage_key = self._loop_stage_key("stage_3_3", iteration)
            self._emit_stage(stage_hook, "started", observe_stage_key)
            latest_observe = await self._agent.complete_json(
                "stage_3_3",
                self._renderer.stage_3_3(
                    stage_2_1,
                    latest_review,
                    latest_probe,
                    iteration,
                    self._max_react_iterations,
                ),
            )
            self._store_loop_stage(stages, "stage_3_3", iteration, latest_observe)
            self._emit_stage(stage_hook, "completed", observe_stage_key, latest_observe)
            iteration_output["sql_observe_output"] = latest_observe
            logger.info(
                "Stage 3.3 SQL observe output, iteration %s:\n%s",
                iteration,
                json.dumps(latest_observe, indent=2),
            )
            decision = (latest_observe.get("react_decision") or {}).get("decision")
            if not self._probe_supports_decision(latest_probe, decision):
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "blocked",
                    "SQL Observe returned a decision that is not supported by SQL Probe evidence.",
                    "stage_3_3",
                    "Inspect the SQL Probe evidence and SQL Observe decision.",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )

            if decision == "stop_success":
                react_iterations.append(iteration_output)
<<<<<<< Updated upstream
=======
                stage_4_input = {"rows": self._probe_raw_output_rows(latest_probe)}
                self._emit_stage(stage_hook, "started", "stage_4")
>>>>>>> Stashed changes
                latest_final = await self._agent.complete_json(
                    "stage_4",
                    self._renderer.stage_4(
                        user_query,
                        stage_2_1,
                        latest_review,
                        latest_probe,
                        latest_observe,
                    ),
                )
                latest_final = self._normalize_stage_4_output(latest_final, latest_probe)
                stages["stage_4"] = latest_final
                self._emit_stage(stage_hook, "completed", "stage_4", latest_final)
                logger.info("Stage 4 final output:\n%s", json.dumps(latest_final, indent=2))
                return self._react_response(
                    user_query,
                    "completed",
                    "SQL was reviewed, executed, and returned data.",
                    "",
                    "",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                    sql_final_output=latest_final,
                )
            if decision == "stop_no_data":
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "no_data",
                    "SQL executed successfully, but no matching database rows were found.",
                    "stage_3_3",
                    "Review the observed no-data evidence before changing the request.",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )
            if decision != "send_to_sql_fix" or not latest_observe.get("send_to_sql_fix"):
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "blocked",
                    "SQL Observe did not return a valid terminal or correction decision.",
                    "stage_3_3",
                    "Inspect SQL Observe output and its ReAct decision.",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )
            if iteration >= self._max_react_iterations:
                react_iterations.append(iteration_output)
                return self._react_response(
                    user_query,
                    "blocked",
                    "The SQL ReAct loop reached its maximum number of correction attempts.",
                    "stage_3_3",
                    "Inspect the loop evidence and unresolved SQL issue.",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )

            fix_stage_key = self._loop_stage_key("stage_3_4", iteration)
            self._emit_stage(stage_hook, "started", fix_stage_key)
            latest_fix = await self._agent.complete_json(
                "stage_3_4",
                self._renderer.stage_3_4(
                    stage_2_1,
                    latest_review,
                    latest_observe,
                    iteration,
                    self._max_react_iterations,
                ),
            )
            self._store_loop_stage(stages, "stage_3_4", iteration, latest_fix)
            self._emit_stage(stage_hook, "completed", fix_stage_key, latest_fix)
            iteration_output["sql_fix_output"] = latest_fix
            logger.info(
                "Stage 3.4 SQL fix output, iteration %s:\n%s",
                iteration,
                json.dumps(latest_fix, indent=2),
            )
            react_iterations.append(iteration_output)
            if (
                latest_fix.get("sql_fix_status") != "fixed"
                or not latest_fix.get("send_back_to_sql_review")
            ):
                return self._react_response(
                    user_query,
                    "blocked",
                    "SQL Fix could not safely produce SQL for another review.",
                    "stage_3_4",
                    "Inspect SQL Fix unresolved issues.",
                    stage_3,
                    latest_review,
                    latest_probe,
                    latest_observe,
                    latest_fix,
                    react_iterations,
                    stages,
                    include_intermediate_stages,
                )
            sql_candidate = latest_fix

        return self._react_response(
            user_query,
            "blocked",
            "The SQL ReAct loop ended without a final decision.",
            "stage_3_3",
            "Inspect the recorded ReAct iterations.",
            stage_3,
            latest_review,
            latest_probe,
            latest_observe,
            latest_fix,
            react_iterations,
            stages,
            include_intermediate_stages,
        )

    @staticmethod
    def _needs_clarification(output: dict[str, Any]) -> bool:
        if output.get("needs_clarification") is True:
            return True
        if output.get("algorithm_status") == "needs_clarification":
            return True
        if output.get("metric_relationship_status") == "needs_clarification":
            return True
        mapped = output.get("MAPPED_JSON_SCHEMA")
        if isinstance(mapped, dict) and mapped.get("needs_clarification") is True:
            return True
        return bool(SqlAgentWorkflow._clarification_question(output)) and (
            output.get("sql_build_status") == "blocked"
            or output.get("sql_review_status") == "blocked"
        )

    @staticmethod
    def _require_keys(stage_name: str, output: dict[str, Any], *keys: str) -> None:
        missing = [key for key in keys if key not in output]
        if not missing:
            return
        logger.error(
            "%s returned JSON missing required keys %s:\n%s",
            stage_name,
            missing,
            json.dumps(output, indent=2),
        )
        missing_text = ", ".join(missing)
        raise ValueError(f"{stage_name} response is missing required key(s): {missing_text}.")

    @staticmethod
    def _clarification_question(output: dict[str, Any]) -> str:
        question = output.get("clarification_question")
        if isinstance(question, str) and question:
            return question
        mapped = output.get("MAPPED_JSON_SCHEMA")
        if (
            isinstance(mapped, dict)
            and isinstance(mapped.get("clarification_question"), str)
            and mapped["clarification_question"]
        ):
            return mapped["clarification_question"]
        clarification = output.get("clarification_required")
        if isinstance(clarification, str) and clarification:
            return clarification
        questions: list[str] = []
        for collection_name in ("calculation_logic_validation", "column_mapping_decisions"):
            collection = output.get(collection_name)
            if not isinstance(collection, list):
                continue
            questions.extend(
                item.get("clarification_required", "")
                for item in collection
                if isinstance(item, dict) and item.get("clarification_required")
            )
        if questions:
            return " ".join(questions)
        return ""

    def _clarification_response(
        self,
        query: str,
        stage_name: str,
        output: dict[str, Any],
        stages: dict[str, dict[str, Any]],
        include_intermediate_stages: bool,
    ) -> PipelineResponse:
        display_stage = stage_name.replace("_", ".")
        clarification_question = self._clarification_question(output)
        return PipelineResponse(
            query=query,
            pipeline_status="needs_clarification",
            message=clarification_question
            or f"Clarification is required at {display_stage} before the pipeline can continue.",
            stopped_at_stage=stage_name,
            clarification_question=clarification_question,
            clarification_fields=build_clarification_fields(
                stage_name, output, clarification_question
            ),
            next_action=(
                "Submit one complete corrected query containing your original requirements "
                "and the clarification answer; the pipeline will rerun from Stage 1."
            ),
            token_count=self._token_count_section(),
            stages=stages if include_intermediate_stages else None,
        )

    @staticmethod
    def _loop_stage_key(stage_name: str, iteration: int) -> str:
        return stage_name if iteration == 1 else f"{stage_name}_iteration_{iteration}"

    @staticmethod
    def _store_loop_stage(
        stages: dict[str, dict[str, Any]],
        stage_name: str,
        iteration: int,
        output: dict[str, Any],
    ) -> None:
        stages[SqlAgentWorkflow._loop_stage_key(stage_name, iteration)] = output

    @staticmethod
    def _probe_supports_decision(sql_probe_output: dict[str, Any], decision: str | None) -> bool:
        results = sql_probe_output.get("execution_results") or {}
        execution_outputs = []
        combined = results.get("combined_sql") or {}
        if combined.get("applicable"):
            execution_outputs.append(combined)
        execution_outputs.extend(results.get("individual_sql_queries") or [])

        if decision == "stop_success":
            return sql_probe_output.get("sql_probe_status") == "success"
        if decision == "stop_no_data":
            return bool(execution_outputs) and all(
                result.get("execution_status") == "success"
                for result in execution_outputs
            ) and any(
                result.get("output_availability") == "no_data" for result in execution_outputs
            )
        if decision == "send_to_sql_fix":
            return bool(sql_probe_output.get("needs_correction")) or any(
                result.get("execution_status") == "failed" for result in execution_outputs
            )
        if decision == "stop_failed":
            return True
        return False

    def _react_response(
        self,
        query: str,
        status: str,
        message: str,
        stopped_at_stage: str,
        next_action: str,
        sql_build_output: dict[str, Any],
        sql_review_output: dict[str, Any] | None,
        sql_probe_output: dict[str, Any] | None,
        sql_observe_output: dict[str, Any] | None,
        sql_fix_output: dict[str, Any] | None,
        react_iterations: list[dict[str, Any]],
        stages: dict[str, dict[str, Any]],
        include_intermediate_stages: bool,
        clarification_question: str = "",
        sql_final_output: dict[str, Any] | None = None,
    ) -> PipelineResponse:
        clarification_fields = None
        if status == "needs_clarification":
            stage_output = sql_review_output if stopped_at_stage == "stage_3_1" else (sql_build_output or {})
            clarification_fields = build_clarification_fields(
                stopped_at_stage,
                stage_output if isinstance(stage_output, dict) else {},
                clarification_question,
            )
        return PipelineResponse(
            query=query,
            pipeline_status=status,
            message=clarification_question or message,
            stopped_at_stage=stopped_at_stage,
            clarification_question=clarification_question,
            clarification_fields=clarification_fields,
            next_action=next_action,
            sql_build_output=sql_build_output,
            sql_review_output=sql_review_output,
            sql_probe_output=sql_probe_output,
            sql_observe_output=sql_observe_output,
            sql_fix_output=sql_fix_output,
            sql_final_output=sql_final_output,
            react_iterations=react_iterations,
            token_count=self._token_count_section(),
            stages=stages if include_intermediate_stages else None,
        )

    def _token_count_section(self) -> dict[str, Any] | None:
        token_count = getattr(self._agent, "token_count", None)
        if token_count is None:
            return None
        return token_count() if callable(token_count) else token_count

    @staticmethod
    def _normalize_stage_4_output(
        sql_final_output: dict[str, Any],
        sql_probe_output: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(sql_final_output)
        raw_rows = SqlAgentWorkflow._probe_raw_output_rows(sql_probe_output)
        if raw_rows:
            normalized["raw_output"] = raw_rows[:5]
        elif isinstance(normalized.get("raw_output"), list):
            normalized["raw_output"] = normalized["raw_output"][:5]
        else:
            normalized["raw_output"] = []
        return normalized

    @staticmethod
    def _probe_raw_output_rows(sql_probe_output: dict[str, Any]) -> list[dict[str, Any]]:
        results = sql_probe_output.get("execution_results") or {}
        raw_rows: list[dict[str, Any]] = []
        combined = results.get("combined_sql") or {}
        if combined.get("applicable"):
            raw_rows.extend(SqlAgentWorkflow._execution_rows(combined))
        for item in results.get("individual_sql_queries") or []:
            metric_name = item.get("metric_name", "")
            for row in SqlAgentWorkflow._execution_rows(item):
                if metric_name:
                    raw_rows.append({"metric_name": metric_name, **row})
                else:
                    raw_rows.append(row)
                if len(raw_rows) >= 5:
                    return raw_rows
        return raw_rows[:5]

    @staticmethod
    def _execution_rows(execution_output: dict[str, Any]) -> list[dict[str, Any]]:
        rows = execution_output.get("table_output") or execution_output.get("sample_output") or []
        return [row for row in rows if isinstance(row, dict)]
