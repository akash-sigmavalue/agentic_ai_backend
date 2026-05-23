"""
Visualization Agent Module 1 — Intent mapping builder for downstream modules.
"""

from typing import Any, Dict

from .constants import MODULE_NAMES
from .helpers import ensure_dict, ensure_list


def build_intent_mapping(output: Dict[str, Any]) -> Dict[str, Any]:
    """
    Creates a self-explaining contract for downstream modules.
    LLM can generate this, but backend also repairs/creates it to avoid missing mapping.
    """
    intent = output.get("structured_intent", {})
    map_req = output.get("map_output_requirements", {})
    sim_req = output.get("simulation_requirements", {})
    what_if_req = output.get("what_if_requirements", {})
    spatial_req = output.get("spatial_requirements", {})
    validation = output.get("validation_status", {})

    primary_metric = intent.get("primary_metric") or map_req.get("base_map_metric") or "selected_metric"
    metric_aggregation = intent.get("metric_aggregation") or map_req.get("map_metric_aggregation") or "auto"
    geography_level = intent.get("geography_level") or map_req.get("geo_level") or "auto"
    selected_map_types = ensure_list(map_req.get("selected_map_types"))

    mapping = ensure_dict(output.get("intent_mapping"))
    mapping.setdefault(
        "mapping_purpose",
        "Explain how the finalized intent fields, dynamic requirement fields, and execution plan should be interpreted by downstream modules.",
    )

    mapping.setdefault(
        "core_field_mapping",
        {
            "primary_metric": {
                "value": primary_metric,
                "meaning": "Main business metric to analyze and visualize.",
                "used_by_modules": [MODULE_NAMES["module_2"], MODULE_NAMES["module_3"], MODULE_NAMES["module_7"]],
            },
            "metric_aggregation": {
                "value": metric_aggregation,
                "meaning": "Aggregation logic to be applied while restructuring data.",
                "used_by_modules": [MODULE_NAMES["module_2"]],
            },
            "geography_level": {
                "value": geography_level,
                "meaning": "Geographic level at which data should be grouped, mapped, and interpreted.",
                "used_by_modules": [MODULE_NAMES["module_2"], MODULE_NAMES["module_3"], MODULE_NAMES["module_6"]],
            },
            "filters": {
                "value": intent.get("filters", {}),
                "meaning": "Filter conditions extracted from the user query.",
                "used_by_modules": [MODULE_NAMES["module_2"]],
            },
            "time_range": {
                "value": intent.get("time_range", {}),
                "meaning": "Time restriction or trend period to be applied if present.",
                "used_by_modules": [MODULE_NAMES["module_2"], MODULE_NAMES["module_3"]],
            },
        },
    )

    dynamic_field_mapping = mapping.get("dynamic_field_mapping", {})
    dynamic_field_mapping.setdefault(
        "selected_map_types",
        {
            "value": selected_map_types,
            "meaning": "Map visualization types selected for the query.",
            "source": "User-specified visualization or backend fallback selection.",
            "used_by_modules": [MODULE_NAMES["module_3"]],
        },
    )
    dynamic_field_mapping.setdefault(
        "primary_map_type",
        {
            "value": map_req.get("primary_map_type"),
            "meaning": "Primary map visualization that should drive the first plotted output.",
            "source": "User visualization intent or inferred fallback logic.",
            "used_by_modules": [MODULE_NAMES["module_3"], MODULE_NAMES["module_7"]],
        },
    )
    if map_req.get("intensity_metric"):
        dynamic_field_mapping.setdefault(
            "intensity_metric",
            {
                "value": map_req.get("intensity_metric"),
                "meaning": "Metric used to control heatmap intensity or visual weight.",
                "source": "Derived from selected metric and map type.",
                "used_by_modules": [MODULE_NAMES["module_3"], MODULE_NAMES["module_6"]],
            },
        )
    if map_req.get("timelapse_required"):
        dynamic_field_mapping.setdefault(
            "timelapse_requirements",
            {
                "value": {
                    "timelapse_required": map_req.get("timelapse_required"),
                    "timelapse_mode": map_req.get("timelapse_mode", "time_slider"),
                    "time_granularity": map_req.get("time_granularity"),
                    "time_field_required": map_req.get("time_field_required"),
                },
                "meaning": "Time-aware map requirement used to build a time-slider or timelapse payload without changing the primary map type.",
                "source": "Derived from user-provided time range or temporal comparison intent.",
                "used_by_modules": [MODULE_NAMES["module_2"], MODULE_NAMES["module_3"], MODULE_NAMES["module_7"]],
            },
        )
    if sim_req.get("is_active"):
        dynamic_field_mapping.setdefault(
            "simulation_requirements",
            {
                "value": sim_req,
                "meaning": "Scenario variables and assumptions required for simulation.",
                "source": "Extracted from simulation intent in user query.",
                "used_by_modules": [MODULE_NAMES["module_4"], MODULE_NAMES["module_7"]],
            },
        )
    if what_if_req.get("is_active"):
        dynamic_field_mapping.setdefault(
            "what_if_requirements",
            {
                "value": what_if_req,
                "meaning": "Base case, changed case, and comparison metric for what-if analysis.",
                "source": "Extracted from what-if intent in user query.",
                "used_by_modules": [MODULE_NAMES["module_5"], MODULE_NAMES["module_7"]],
            },
        )
    if spatial_req.get("is_active"):
        dynamic_field_mapping.setdefault(
            "spatial_requirements",
            {
                "value": spatial_req,
                "meaning": "Spatial operation and contextual map analysis requirements.",
                "source": "Extracted from spatial/proximity/hotspot intent in user query.",
                "used_by_modules": [MODULE_NAMES["module_6"], MODULE_NAMES["module_7"]],
            },
        )
    mapping["dynamic_field_mapping"] = dynamic_field_mapping

    mapping.setdefault(
        "module_usage_mapping",
        {
            MODULE_NAMES["module_2"]: ["Use structured_intent, filters, metric_aggregation, geography_level, time_range, and intent_mapping to create analysis_ready_dataset."],
            MODULE_NAMES["module_3"]: ["Use map_output_requirements, selected_map_types, base_map_metric, geo_level, tooltip_fields, and layer_requirements to create plotted_map_output."],
            MODULE_NAMES["module_4"]: ["Use simulation_requirements only when is_active is true."],
            MODULE_NAMES["module_5"]: ["Use what_if_requirements only when is_active is true and compare base vs changed output."],
            MODULE_NAMES["module_6"]: ["Use plotted_map_output and spatial_requirements when spatial analysis is active."],
            MODULE_NAMES["module_7"]: ["Use all successful module outputs and validation_status to generate final business response."],
        },
    )

    mapping.setdefault(
        "field_dependency_mapping",
        {
            "map_output": {
                "depends_on": ["primary_metric", "geography_level", "selected_map_types"],
                "created_in_module": MODULE_NAMES["module_3"],
                "used_in_modules": [MODULE_NAMES["module_6"], MODULE_NAMES["module_7"]],
            },
            "analysis_ready_dataset": {
                "depends_on": ["retrieved_dataset", "structured_intent", "mapped_field_config"],
                "created_in_module": MODULE_NAMES["module_2"],
                "used_in_modules": [MODULE_NAMES["module_3"], MODULE_NAMES["module_4"], MODULE_NAMES["module_5"], MODULE_NAMES["module_7"]],
            },
        },
    )

    mapping.setdefault(
        "fallback_mapping",
        {
            "visualization_auto_selected": validation.get("visualization_auto_selected", False),
            "fallback_applied": validation.get("fallback_applied", False),
            "fallback_reason": validation.get("fallback_reason", ""),
        },
    )

    mapping.setdefault(
        "notes_for_downstream_modules",
        [
            "The outer schema is fixed, but inner requirement blocks may contain dynamic fields.",
            "Use additional_parameters in each requirement block for query-specific fields that are not part of the minimum schema.",
            "Do not reject a block only because it contains extra fields; validate only minimum required fields.",
            "If coordinates are missing, Module 3 should first try available location hierarchy or static mapping before asking for clarification.",
            "If time_field_required is true, Module 2 must preserve a date/year/quarter/month field during restructuring.",
            "If timelapse_required is true, Module 3 must generate a time-slider/timelapse-ready map payload while keeping the selected primary map type unchanged.",
        ],
    )

    return mapping
