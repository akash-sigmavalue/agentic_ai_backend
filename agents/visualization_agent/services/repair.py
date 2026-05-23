"""
Visualization Agent Module 1 — Master validate_and_repair_output function.
"""

from typing import Any, Dict

from .constants import CORE_OUTPUT_KEYS, MODULE_NAMES, SUPPORTED_MAP_TYPES
from .helpers import ensure_block_defaults, ensure_dict, ensure_list, normalize_active_flag
from .time_detection import apply_time_requirements_to_map
from .map_logic import (
    detect_explicit_map_type,
    infer_default_map_type,
    update_layer_flags,
    validate_minimum_block_fields,
)
from .execution_plan import validate_and_repair_execution_plan
from .intent_mapping import build_intent_mapping


def validate_and_repair_output(output: Dict[str, Any], user_query: str) -> Dict[str, Any]:
    """
    Applies finalized Module 1 rules:
    - Fixed outer schema + flexible inner blocks.
    - Map output is mandatory.
    - Basic queries are not invalid if visualization type is missing.
    - Fallback map selection is auto-applied.
    - intent_mapping is added for downstream explanation of dynamic fields.
    """
    output = ensure_block_defaults(output, user_query)

    intent = output["structured_intent"]
    intent.setdefault("output_expectation", "map_based_visualization")

    map_req = output["map_output_requirements"]
    map_req["is_active"] = True
    map_req.setdefault("is_map_output_required", True)
    map_req.setdefault("additional_parameters", {})

    selected_map_types = ensure_list(map_req.get("selected_map_types"))
    selected_map_types = [m for m in selected_map_types if isinstance(m, str) and m in SUPPORTED_MAP_TYPES]
    explicit_map_type, explicit_reason = detect_explicit_map_type(output)

    if explicit_map_type:
        map_req["visualization_intent_provided"] = True
        map_req["visualization_auto_selected"] = False
        map_req["explicit_map_type_detected"] = True
        map_req["explicit_map_type_reason"] = explicit_reason
        map_req["auto_selected_reason"] = ""
        selected_map_types = [explicit_map_type]
        map_req["primary_map_type"] = explicit_map_type
        map_req["secondary_map_types"] = []
        map_req["layer_requirements"] = {}
    elif not selected_map_types:
        fallback_map_type, fallback_reason = infer_default_map_type(output)
        map_req["visualization_intent_provided"] = False
        map_req["visualization_auto_selected"] = True
        map_req["auto_selected_reason"] = fallback_reason
        selected_map_types = [fallback_map_type]
        map_req["primary_map_type"] = fallback_map_type
    else:
        map_req.setdefault("visualization_intent_provided", True)
        map_req.setdefault("visualization_auto_selected", False)
        map_req.setdefault("primary_map_type", selected_map_types[0])

    primary_map_type = map_req.get("primary_map_type")
    if primary_map_type not in selected_map_types:
        primary_map_type = selected_map_types[0]
        map_req["primary_map_type"] = primary_map_type

    map_req["selected_map_types"] = selected_map_types
    map_req.setdefault("secondary_map_types", selected_map_types[1:])
    map_req.setdefault("base_map_metric", intent.get("primary_metric", "selected_metric"))
    map_req.setdefault("map_metric_aggregation", intent.get("metric_aggregation", "auto"))
    map_req.setdefault("geo_level", intent.get("geography_level", "auto"))
    map_req.setdefault("location_fields_required", ["latitude", "longitude"])
    map_req.setdefault("optional_location_fields", ["project_name", "location_name", "village", "micromarket", "city"])
    map_req.setdefault("tooltip_fields", [])
    map_req.setdefault("time_field_required", bool(intent.get("time_dimension") or intent.get("time_range") or intent.get("time_period")))
    map_req.setdefault("time_granularity", intent.get("time_dimension") or None)
    output["map_output_requirements"] = map_req

    output = apply_time_requirements_to_map(output, user_query)
    map_req = update_layer_flags(output["map_output_requirements"])
    output["map_output_requirements"] = map_req

    output["simulation_requirements"] = normalize_active_flag(output.get("simulation_requirements"), default=False)
    output["what_if_requirements"] = normalize_active_flag(output.get("what_if_requirements"), default=False)
    output["spatial_requirements"] = normalize_active_flag(output.get("spatial_requirements"), default=False)
    output["insight_requirements"] = normalize_active_flag(output.get("insight_requirements"), default=True)

    active_blocks = ["map_output_requirements"]
    if output["simulation_requirements"].get("is_active") is True:
        active_blocks.append("simulation_requirements")
    if output["what_if_requirements"].get("is_active") is True:
        active_blocks.append("what_if_requirements")
    if output["spatial_requirements"].get("is_active") is True:
        active_blocks.append("spatial_requirements")
    output["active_requirement_blocks"] = active_blocks

    required_modules = [MODULE_NAMES["module_2"], MODULE_NAMES["module_3"]]
    if output["simulation_requirements"].get("is_active") is True:
        required_modules.append(MODULE_NAMES["module_4"])
    if output["what_if_requirements"].get("is_active") is True:
        required_modules.append(MODULE_NAMES["module_5"])
    if output["spatial_requirements"].get("is_active") is True:
        required_modules.append(MODULE_NAMES["module_6"])
    required_modules.append(MODULE_NAMES["module_7"])
    output["required_modules"] = required_modules

    output["execution_flags"] = {
        "requires_data_restructuring": True,
        "requires_geo": True,
        "requires_map_plotting": True,
        "requires_simulation": output["simulation_requirements"].get("is_active", False),
        "requires_what_if": output["what_if_requirements"].get("is_active", False),
        "requires_spatial": output["spatial_requirements"].get("is_active", False),
        "requires_insight": True,
    }

    missing_fields = validate_minimum_block_fields(output)

    output["validation_status"] = {
        "is_valid": len(missing_fields) == 0,
        "minimum_visualization_requirement_met": True,
        "hybrid_schema_applied": True,
        "fixed_outer_schema_keys": CORE_OUTPUT_KEYS,
        "dynamic_inner_blocks_allowed": True,
        "visualization_intent_provided": map_req.get("visualization_intent_provided", False),
        "visualization_auto_selected": map_req.get("visualization_auto_selected", False),
        "active_requirement_blocks": active_blocks,
        "clarification_required": False,
        "missing_fields": missing_fields,
        "fallback_applied": map_req.get("visualization_auto_selected", False),
        "fallback_reason": map_req.get("auto_selected_reason", ""),
    }

    output = validate_and_repair_execution_plan(output)
    output["intent_mapping"] = build_intent_mapping(output)

    return output
