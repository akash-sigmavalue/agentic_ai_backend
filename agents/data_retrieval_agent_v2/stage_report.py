"""Build full pipeline payloads for Word / document export."""

from __future__ import annotations

from typing import Any

from .models import PipelineResponse
from .stage_catalog import STAGE_BY_ID, stage_display_label


def _stage_sort_key(stage_name: str) -> float:
    base = stage_name.split("_iteration_")[0]
    order = float((STAGE_BY_ID.get(base) or {}).get("order", 99))
    if "_iteration_" in stage_name:
        suffix = stage_name.rsplit("_iteration_", 1)[1]
        if suffix.isdigit():
            order += (int(suffix) - 1) * 0.1
    return order


def build_stage_report_payload(response: PipelineResponse) -> dict[str, Any]:
    stages = response.stages or {}
    ordered_stage_names = sorted(stages.keys(), key=_stage_sort_key)
    return {
        "query": response.query,
        "pipeline_status": response.pipeline_status,
        "message": response.message,
        "stopped_at_stage": response.stopped_at_stage,
        "clarification_question": response.clarification_question,
        "token_count": response.token_count,
        "stages": stages,
        "ordered_stage_names": ordered_stage_names,
        "stage_labels": {name: stage_display_label(name) for name in stages},
        "react_iterations": response.react_iterations or [],
        "sql_build_output": response.sql_build_output,
        "sql_review_output": response.sql_review_output,
        "sql_probe_output": response.sql_probe_output,
        "sql_observe_output": response.sql_observe_output,
        "sql_fix_output": response.sql_fix_output,
        "sql_final_output": response.sql_final_output,
    }
