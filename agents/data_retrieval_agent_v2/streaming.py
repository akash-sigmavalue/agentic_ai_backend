from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from .config import get_settings
from .llm import OpenAIJsonAgent
from .models import GenerateSqlRequest, PipelineResponse
from .sql_probe import SqlProbeService
from .workflow import SqlAgentWorkflow


def _sse(event_type: str, content: Any, **kwargs: Any) -> str:
    payload = {"type": event_type, "content": content, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _stage_label(stage_name: str) -> str:
    labels = {
        "stage_1": "Stage 1 - Intent Parsing",
        "stage_1_5": "Stage 1.5 - Metric Mapping",
        "stage_1_6": "Stage 1.6 - Metric Validation",
        "stage_2": "Stage 2 - Schema Planning",
        "stage_2_1": "Stage 2.1 - Semantic Resolution",
        "stage_3": "Stage 3 - SQL Build",
        "stage_3_1": "Stage 3.1 - SQL Review",
        "stage_3_2": "Stage 3.2 - SQL Probe",
        "stage_3_3": "Stage 3.3 - SQL Observe",
        "stage_3_4": "Stage 3.4 - SQL Fix",
        "stage_4": "Stage 4 - Final Answer",
    }
    base_name = stage_name.split("_iteration_")[0]
    suffix = ""
    if "_iteration_" in stage_name:
        suffix = f" (Iteration {stage_name.rsplit('_iteration_', 1)[1]})"
    return f"{labels.get(base_name, stage_name.replace('_', '.'))}{suffix}"


def _execution_outputs(probe_output: dict[str, Any] | None) -> list[dict[str, Any]]:
    results = (probe_output or {}).get("execution_results") or {}
    outputs: list[dict[str, Any]] = []
    combined = results.get("combined_sql") or {}
    if combined.get("applicable"):
        outputs.append({"label": "Combined SQL", "domain": "v2-combined", **combined})
    for index, result in enumerate(results.get("individual_sql_queries") or [], start=1):
        metric = result.get("metric_name") or f"metric_{index}"
        outputs.append({"label": metric, "domain": f"v2-{metric}", **result})
    return outputs


def _result_set(output: dict[str, Any]) -> dict[str, Any] | None:
    rows = output.get("table_output") or output.get("sample_output") or []
    if not rows:
        return None
    columns = list({column for row in rows if isinstance(row, dict) for column in row.keys()})
    if not columns:
        return None
    return {
        "title": output.get("label") or "SQL Result",
        "domain": output.get("domain") or "data_retrieval_agent_v2",
        "columns": columns,
        "rows": rows,
    }


def _final_markdown(response: PipelineResponse) -> str:
    final = response.sql_final_output or {}
    parts = [response.message]
    layman_output = final.get("layman_output")
    if layman_output:
        parts.append(str(layman_output))
    insights = final.get("insights")
    if isinstance(insights, list) and insights:
        parts.append("**Insights**\n" + "\n".join(f"- {insight}" for insight in insights))
    steps = final.get("steps_of_arriving_at_output")
    if isinstance(steps, list) and steps:
        parts.append("**Steps**\n" + "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps)))
    return "\n\n".join(part for part in parts if part).strip()


class DataRetrievalAgentV2StreamAdapter:
    """Adapts the v2 SQL pipeline response to the existing data retrieval SSE UI."""

    async def execute_stream(
        self,
        question: str,
        session_id: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        started = time.perf_counter()
        effective_session_id = session_id or str(uuid.uuid4())
        yield _sse("session", {"session_id": effective_session_id})
        yield _sse("start", f"Processing with data retrieval agent v2: {question}")

        try:
            settings = get_settings()
            request = GenerateSqlRequest(
                query=question,
                model=model,
                include_intermediate_stages=True,
            )
            workflow = SqlAgentWorkflow(
                OpenAIJsonAgent(settings, model=request.model),
                sql_probe=SqlProbeService(settings),
                max_react_iterations=settings.react_max_iterations,
            )
            response = await workflow.run(
                request.query,
                request.include_intermediate_stages,
                request.semantic_context.model_dump(),
            )
        except Exception as exc:
            yield _sse("error", f"data_retrieval_agent_v2 failed: {exc}")
            yield _sse("done", "", metrics={"duration_seconds": round(time.perf_counter() - started, 2), "total_tokens": 0})
            return

        for stage_name, output in (response.stages or {}).items():
            yield _sse("stage", _stage_label(stage_name))
            yield _sse("debug_trace", {"step": stage_name, "phase": "completed", "summary": json.dumps(output, default=str)})

        token_count = response.token_count or {}
        cumulative_tokens = 0
        for usage in token_count.get("stages") or []:
            total_tokens = int(usage.get("total_tokens") or 0)
            cumulative_tokens += total_tokens
            yield _sse(
                "token_usage",
                {
                    "stage": usage.get("stage") or "unknown",
                    "prompt_tokens": usage.get("prompt_tokens") or 0,
                    "completion_tokens": usage.get("completion_tokens") or 0,
                    "total_tokens": total_tokens,
                    "cumulative_total_tokens": cumulative_tokens,
                    "cumulative_cost_usd": 0,
                },
            )

        if response.pipeline_status == "needs_clarification":
            question_text = response.clarification_question or "Please clarify the requested values."
            yield _sse(
                "clarification_required",
                {
                    "message": response.message,
                    "questions": [question_text],
                    "clarification_type": "v2_pipeline",
                    "original_query": response.query,
                    "stopped_at_stage": response.stopped_at_stage,
                    "next_action": response.next_action,
                },
            )
        else:
            for output in _execution_outputs(response.sql_probe_output):
                result_set = _result_set(output)
                if result_set:
                    yield _sse("result_set", result_set)
            yield _sse("report_chunk", f"{_final_markdown(response)}\n")

        total_tokens = token_count.get("total_tokens") or cumulative_tokens
        yield _sse(
            "done",
            "",
            metrics={
                "duration_seconds": round(time.perf_counter() - started, 2),
                "total_tokens": total_tokens,
                "pipeline_status": response.pipeline_status,
            },
        )
