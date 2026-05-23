"""
Visualization Agent Module 1 — Deterministic execution plan builder.
"""

from typing import Any, Dict, List

from .constants import MODULE_NAMES
from .helpers import ensure_dict, ensure_list


def _active(output: Dict[str, Any], block_name: str) -> bool:
    """Convenience helper for requirement block activation checks."""
    return bool(ensure_dict(output.get(block_name)).get("is_active", False))


def _plan_status(is_required: bool) -> str:
    return "planned" if is_required else "skipped"


def _conditional_skip(condition_text: str, is_required: bool) -> str:
    if is_required:
        return "Do not skip because this requirement is active for the current user query."
    return condition_text


def validate_and_repair_execution_plan(output: Dict[str, Any]) -> Dict[str, Any]:
    """Builds a deterministic, detailed, downstream-safe execution plan."""
    output["execution_plan"] = build_default_execution_plan(output)
    return output


def build_default_execution_plan(output: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Create the standard workflow plan for the Visualization Agent."""
    map_req = ensure_dict(output.get("map_output_requirements"))
    sim_active = _active(output, "simulation_requirements")
    what_if_active = _active(output, "what_if_requirements")
    spatial_active = _active(output, "spatial_requirements")

    selected_map_types = ensure_list(map_req.get("selected_map_types"))
    primary_map_type = map_req.get("primary_map_type", "")
    timelapse_required = bool(map_req.get("timelapse_required", False))

    map_validation_inputs = ["analysis_ready_dataset", "map_output_requirements", "intent_mapping"]
    map_validation_depends = [5]
    if sim_active:
        map_validation_inputs.append("simulated_dataset")
        map_validation_depends.append(6)
    if what_if_active:
        map_validation_inputs.append("what_if_comparison_output")
        map_validation_depends.append(7)

    insight_inputs = [
        "validated_intent_json",
        "analysis_ready_dataset",
        "plotted_map_output",
        "insight_requirements",
        "intent_mapping",
    ]
    insight_depends = [10]
    if sim_active:
        insight_inputs.append("simulated_dataset")
        insight_depends.append(6)
    if what_if_active:
        insight_inputs.append("what_if_comparison_output")
        insight_depends.append(7)
    if spatial_active:
        insight_inputs.append("spatial_analysis_output")
        insight_depends.append(11)

    return [
        {
            "step_id": 1,
            "step_name": "Finalize and validate user intent",
            "module": MODULE_NAMES["module_1"],
            "step_purpose": "Confirm the business objective, metric, geography, filters, time requirement, map output requirement, and downstream workflow route.",
            "action_type": "intent_validation",
            "input_required": ["user_query", "structured_intent", "request_classification", "map_output_requirements"],
            "expected_output": "validated_intent_json_with_requirement_blocks",
            "depends_on": [],
            "validation_checks": [
                "business_objective is present or inferable",
                "structured_intent is available",
                "map_output_requirements.is_active is true",
                "at least one selected_map_type is present",
                "required_modules and execution_flags are consistent with active requirement blocks",
            ],
            "skip_condition": "Never skip. This is the first mandatory workflow step.",
            "failure_handling": "If minimum intent cannot be inferred, set clarification_required=true and stop before data processing.",
            "status": "planned",
        },
        {
            "step_id": 2,
            "step_name": "Receive retrieved data from Data Retrieval Agent",
            "module": MODULE_NAMES["module_2"],
            "step_purpose": "Accept the dataset already retrieved by the Data Retrieval Agent and prepare it for compatibility checks.",
            "action_type": "data_input",
            "input_required": ["retrieved_dataset", "column_metadata", "validated_intent_json"],
            "expected_output": "raw_dataset_for_visualization_agent",
            "depends_on": [1],
            "validation_checks": [
                "retrieved_dataset is available",
                "dataset has rows and columns",
                "dataset is readable as tabular or structured records",
                "column_metadata is available or can be inferred",
            ],
            "skip_condition": "Never skip when the user expects a data-backed visualization.",
            "failure_handling": "Return data_missing status if the retrieved dataset is empty or unavailable.",
            "status": "planned",
        },
        {
            "step_id": 3,
            "step_name": "Map retrieved columns to intent requirements",
            "module": MODULE_NAMES["module_2"],
            "step_purpose": "Map source columns to the fields required by the finalized intent.",
            "action_type": "schema_mapping",
            "input_required": ["raw_dataset_for_visualization_agent", "structured_intent", "map_output_requirements", "intent_mapping"],
            "expected_output": "mapped_field_config",
            "depends_on": [2],
            "validation_checks": [
                "metric/source measure field is mapped or derivable",
                "location/geography field is mapped or derivable",
                "time field is mapped when time_field_required is true",
                "filter fields are mapped when filters are present",
                "map tooltip candidate fields are identified where possible",
            ],
            "skip_condition": "Skip only if the retrieved dataset already follows the standard visualization schema.",
            "failure_handling": "Use intent_mapping fallback candidates; if still unresolved, mark missing_field_requirements for the conversational clarification layer.",
            "status": "planned",
        },
        {
            "step_id": 4,
            "step_name": "Filter dataset based on finalized intent",
            "module": MODULE_NAMES["module_2"],
            "step_purpose": "Apply user-intent filters such as location, property segment, project, date range, price range, or category.",
            "action_type": "filtering",
            "input_required": ["raw_dataset_for_visualization_agent", "mapped_field_config", "structured_intent.filters"],
            "expected_output": "filtered_dataset",
            "depends_on": [3],
            "validation_checks": [
                "all filter fields exist or have valid fallback mappings",
                "time filters are applied when time_field_required is true",
                "filtered_dataset is not empty",
                "excluded records are counted or logged for transparency",
            ],
            "skip_condition": "Skip only if the user query contains no filters and full retrieved data should be used.",
            "failure_handling": "If filtering returns no records, return filter_no_result with filter summary and candidate fallback values.",
            "status": "planned",
        },
        {
            "step_id": 5,
            "step_name": "Aggregate and restructure data for visualization",
            "module": MODULE_NAMES["module_2"],
            "step_purpose": "Convert filtered data into an analysis-ready dataset at the required metric, geography, dimension, and time granularity.",
            "action_type": "aggregation_restructuring",
            "input_required": ["filtered_dataset", "mapped_field_config", "structured_intent", "map_output_requirements", "intent_mapping"],
            "expected_output": "analysis_ready_dataset",
            "depends_on": [4],
            "validation_checks": [
                "aggregation level matches geo_level",
                "base_map_metric is computed or derivable",
                "time buckets are created when time_field_required is true",
                "timelapse frame key is created when timelapse_required is true",
                "analysis_ready_dataset contains usable records for map plotting",
            ],
            "skip_condition": "Skip aggregation only if the selected map type requires raw row-level plotting.",
            "failure_handling": "If metric aggregation fails, return aggregation_failed and include missing numeric/date fields.",
            "status": "planned",
        },
        {
            "step_id": 6,
            "step_name": "Run simulation if required",
            "module": MODULE_NAMES["module_4"],
            "step_purpose": "Apply scenario assumptions and create a simulated dataset when simulation_requirements is active.",
            "action_type": "simulation",
            "input_required": ["analysis_ready_dataset", "simulation_requirements", "intent_mapping"] if sim_active else ["simulation_requirements"],
            "expected_output": "simulated_dataset" if sim_active else "simulation_not_required",
            "depends_on": [5] if sim_active else [1],
            "validation_checks": [
                "simulation_requirements.is_active is true",
                "scenario variable is mapped to a valid field",
                "target metric is numeric or derivable",
                "simulation operation is supported by first-iteration safe operations",
            ] if sim_active else ["simulation_requirements.is_active is false"],
            "skip_condition": _conditional_skip("Skip because simulation_requirements.is_active is false.", sim_active),
            "failure_handling": "If unsupported, return simulation_skipped_or_unsupported and continue with base analysis_ready_dataset.",
            "status": _plan_status(sim_active),
        },
        {
            "step_id": 7,
            "step_name": "Run what-if comparison if required",
            "module": MODULE_NAMES["module_5"],
            "step_purpose": "Compare the base dataset with a changed or simulated scenario when what_if_requirements is active.",
            "action_type": "what_if_comparison",
            "input_required": ["analysis_ready_dataset", "simulated_dataset", "what_if_requirements", "intent_mapping"] if what_if_active else ["what_if_requirements"],
            "expected_output": "what_if_comparison_output" if what_if_active else "what_if_not_required",
            "depends_on": ([5, 6] if sim_active else [5]) if what_if_active else [1],
            "validation_checks": [
                "what_if_requirements.is_active is true",
                "base case and changed case are available",
                "comparison metric is numeric or derivable",
                "comparison level matches geography/time requirements",
            ] if what_if_active else ["what_if_requirements.is_active is false"],
            "skip_condition": _conditional_skip("Skip because what_if_requirements.is_active is false.", what_if_active),
            "failure_handling": "If comparison cannot be computed, return what_if_skipped_or_unsupported and continue with available outputs.",
            "status": _plan_status(what_if_active),
        },
        {
            "step_id": 8,
            "step_name": "Validate map output requirements against prepared data",
            "module": MODULE_NAMES["module_3"],
            "step_purpose": "Confirm that the selected map type, metric, geo level, time/timelapse requirement, and layer requirements can be produced from the prepared data.",
            "action_type": "map_requirement_validation",
            "input_required": map_validation_inputs,
            "expected_output": "validated_map_requirements",
            "depends_on": map_validation_depends,
            "validation_checks": [
                "primary_map_type is supported",
                "base_map_metric exists in analysis or scenario output",
                "geo_level is available or mappable",
                "time field exists when time_field_required is true",
                "timelapse frame field exists when timelapse_required is true",
                "layer_requirements are internally consistent with selected_map_types",
            ],
            "skip_condition": "Never skip because map output is mandatory for this Visualization Agent.",
            "failure_handling": "Fallback to the simplest valid map type, usually marker_map or 2d_heatmap.",
            "status": "planned",
        },
        {
            "step_id": 9,
            "step_name": "Prepare geo-compatible plotting data",
            "module": MODULE_NAMES["module_3"],
            "step_purpose": "Convert analysis/scenario output into map-compatible records with coordinates, geometry, metric values, tooltip fields, and optional time frames.",
            "action_type": "geo_data_preparation",
            "input_required": ["validated_map_requirements", "analysis_ready_dataset", "mapped_field_config", "intent_mapping"],
            "expected_output": "map_compatible_dataset",
            "depends_on": [8],
            "validation_checks": [
                "latitude/longitude or boundary/centroid is available or mappable",
                "each map feature has a metric value",
                "tooltip fields are available or generated from mapped fields",
                "time frame field is preserved for time-aware maps",
                "records are grouped at the intended geo_level",
            ],
            "skip_condition": "Skip only if analysis_ready_dataset is already in the finalized frontend map contract.",
            "failure_handling": "Use static location lookup or centroid fallback.",
            "status": "planned",
        },
        {
            "step_id": 10,
            "step_name": "Generate plotted map output",
            "module": MODULE_NAMES["module_3"],
            "step_purpose": "Generate the actual first-iteration plotted map output/configuration.",
            "action_type": "map_generation",
            "input_required": [
                "map_compatible_dataset",
                "selected_map_types",
                "primary_map_type",
                "base_map_metric",
                "layer_requirements",
                "timelapse_settings" if timelapse_required else "map_settings",
            ],
            "expected_output": "plotted_map_output",
            "depends_on": [9],
            "validation_checks": [
                f"selected map types are supported: {selected_map_types}",
                f"primary map type is generated: {primary_map_type}",
                "map layer data follows frontend contract",
                "tooltip metadata is attached",
                "time-slider/timelapse payload is attached" if timelapse_required else "static map payload is valid",
            ],
            "skip_condition": "Never skip because a visual/map output is required for every user query.",
            "failure_handling": "Fallback to marker_map with metric annotations if the selected map output cannot be generated.",
            "status": "planned",
        },
        {
            "step_id": 11,
            "step_name": "Run spatial analysis if required",
            "module": MODULE_NAMES["module_6"],
            "step_purpose": "Analyze geographic patterns from the plotted map output when spatial_requirements is active.",
            "action_type": "spatial_analysis",
            "input_required": ["plotted_map_output", "spatial_requirements", "intent_mapping"] if spatial_active else ["spatial_requirements"],
            "expected_output": "spatial_analysis_output" if spatial_active else "spatial_analysis_not_required",
            "depends_on": [10] if spatial_active else [1],
            "validation_checks": [
                "spatial_requirements.is_active is true",
                "plotted_map_output is available",
                "geo fields and metric values are valid",
                "required contextual layers are available or can be deferred",
            ] if spatial_active else ["spatial_requirements.is_active is false"],
            "skip_condition": _conditional_skip("Skip because spatial_requirements.is_active is false.", spatial_active),
            "failure_handling": "If advanced context layers are unavailable, generate basic spatial interpretation from plotted map output.",
            "status": _plan_status(spatial_active),
        },
        {
            "step_id": 12,
            "step_name": "Generate final insight summary",
            "module": MODULE_NAMES["module_7"],
            "step_purpose": "Generate the final user-facing response by combining the intent, prepared data, plotted map output, and any simulation/what-if/spatial outputs that were executed.",
            "action_type": "insight_generation",
            "input_required": insight_inputs,
            "expected_output": "final_visualization_agent_response",
            "depends_on": sorted(set(insight_depends)),
            "validation_checks": [
                "final response is aligned with business_objective",
                "insights are supported by computed outputs",
                "map output is included or referenced",
                "skipped modules are not treated as executed outputs",
                "limitations/fallbacks are mentioned when relevant",
            ],
            "skip_condition": "Never skip because final user-facing response is required.",
            "failure_handling": "Generate a limited response from available successful outputs and clearly state missing/failed workflow parts.",
            "status": "planned",
        },
    ]
