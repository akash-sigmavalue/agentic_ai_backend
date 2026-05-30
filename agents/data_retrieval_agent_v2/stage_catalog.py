"""Canonical v2 pipeline stages aligned with promt.py prompt stages."""

from __future__ import annotations

from typing import Any

STAGE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "stage_1",
        "order": 1,
        "title": "Stage 1",
        "subtitle": "Intent parsing & schema mapping",
        "icon": "🎯",
        "prompt_key": "stage_1",
        "output_keys": ["OUTPUT_JSON_SCHEMA", "MAPPED_JSON_SCHEMA"],
    },
    {
        "id": "stage_1_5",
        "order": 2,
        "title": "Stage 1.5",
        "subtitle": "Metric completeness & meaning",
        "icon": "📊",
        "prompt_key": "stage_1_5",
        "output_keys": ["metric_completeness_status", "final_metrics_list", "MAPPED_JSON_SCHEMA"],
    },
    {
        "id": "stage_1_6",
        "order": 3,
        "title": "Stage 1.6",
        "subtitle": "Metric calculation relationship",
        "icon": "🔗",
        "prompt_key": "stage_1_6",
        "output_keys": ["metric_relationship_status", "metric_relationship", "MAPPED_JSON_SCHEMA"],
    },
    {
        "id": "stage_2",
        "order": 4,
        "title": "Stage 2",
        "subtitle": "Algorithm creation",
        "icon": "🧮",
        "prompt_key": "stage_2",
        "output_keys": ["algorithm_status", "final_algorithm_structure"],
    },
    {
        "id": "stage_2_1",
        "order": 5,
        "title": "Stage 2.1",
        "subtitle": "Semantic resolution",
        "icon": "🗂️",
        "prompt_key": "stage_2_1",
        "output_keys": ["algorithm_status", "final_algorithm_structure"],
    },
    {
        "id": "stage_3",
        "order": 6,
        "title": "Stage 3",
        "subtitle": "SQL build",
        "icon": "🛠️",
        "prompt_key": "stage_3",
        "output_keys": ["sql_build_status", "generated_sql"],
    },
    {
        "id": "stage_3_1",
        "order": 7,
        "title": "Stage 3.1",
        "subtitle": "SQL review",
        "icon": "🔍",
        "prompt_key": "stage_3_1",
        "output_keys": ["sql_review_status"],
    },
    {
        "id": "stage_3_2",
        "order": 8,
        "title": "Stage 3.2",
        "subtitle": "SQL probe execution",
        "icon": "🧪",
        "prompt_key": "stage_3_2",
        "output_keys": ["execution_results"],
    },
    {
        "id": "stage_3_3",
        "order": 9,
        "title": "Stage 3.3",
        "subtitle": "SQL observe & ReAct decision",
        "icon": "👁️",
        "prompt_key": "stage_3_3",
        "output_keys": ["sql_observe_status", "react_decision"],
    },
    {
        "id": "stage_3_4",
        "order": 10,
        "title": "Stage 3.4",
        "subtitle": "SQL fix",
        "icon": "🔧",
        "prompt_key": "stage_3_4",
        "output_keys": ["sql_fix_status"],
    },
    {
        "id": "stage_4",
        "order": 11,
        "title": "Stage 4",
        "subtitle": "Final answer synthesis",
        "icon": "✅",
        "prompt_key": "stage_4",
        "output_keys": ["layman_output", "insights", "steps_of_arriving_at_output"],
    },
]

STAGE_BY_ID = {item["id"]: item for item in STAGE_CATALOG}


def base_stage_id(stage_name: str) -> str:
    return stage_name.split("_iteration_")[0]


def stage_display_label(stage_name: str) -> str:
    entry = stage_catalog_entry(stage_name)
    title = entry.get("title") or stage_name.replace("_", ".")
    subtitle = entry.get("subtitle") or ""
    return f"{title} — {subtitle}" if subtitle else title


def stage_catalog_entry(stage_name: str) -> dict[str, Any]:
    base = base_stage_id(stage_name)
    entry = dict(STAGE_BY_ID.get(base, {}))
    if "_iteration_" in stage_name:
        iteration = stage_name.rsplit("_iteration_", 1)[1]
        entry["title"] = f"{entry.get('title', base)} · Iteration {iteration}"
        entry["iteration"] = int(iteration) if iteration.isdigit() else iteration
        if iteration.isdigit():
            entry["order"] = float(entry.get("order", 99)) + (int(iteration) - 1) * 0.1
    entry["id"] = stage_name
    entry["base_id"] = base
    return entry
