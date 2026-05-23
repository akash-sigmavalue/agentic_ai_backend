"""
Visualization Agent Module 1 — Constants and configuration.
"""

DEFAULT_MODEL = "gpt-5.4-mini"

MODEL_PRICING_USD_PER_1M_TOKENS = {
    "gpt-5.5": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
    "gpt-5.4-mini": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
}

SUPPORTED_MAP_TYPES = [
    "marker_map",
    "cluster_map",
    "2d_overlay",
    "2d_heatmap",
    "region_choropleth",
    "3d_building_plotting",
    "3d_floor_wise",
    "3d_heatmap",
    "3d_timelapse",
    "proximity_map",
    "comparison_map",
]

MODULE_NAMES = {
    "module_1": "Intent Finalization & Visualization Planning",
    "module_2": "Data Restructuring & Filtering",
    "module_3": "Geo-Enrichment & Map Plotting",
    "module_4": "Simulation Depiction Layer",
    "module_5": "What-if Analysis Engine",
    "module_6": "Spatial Analysis",
    "module_7": "Insight Generation",
}

CORE_OUTPUT_KEYS = [
    "module_number",
    "module_name",
    "module_purpose",
    "user_query",
    "business_objective",
    "structured_intent",
    "request_classification",
    "execution_flags",
    "active_requirement_blocks",
    "map_output_requirements",
    "simulation_requirements",
    "what_if_requirements",
    "spatial_requirements",
    "insight_requirements",
    "required_modules",
    "execution_plan",
    "validation_status",
    "intent_mapping",
]

EXECUTION_PLAN_REQUIRED_FIELDS = [
    "step_id",
    "step_name",
    "module",
    "step_purpose",
    "action_type",
    "input_required",
    "expected_output",
    "depends_on",
    "validation_checks",
    "skip_condition",
    "failure_handling",
    "status",
]

DEFAULT_LAYER_REQUIREMENTS = {
    "needs_2d_layer": False,
    "needs_3d_layer": False,
    "needs_heatmap_layer": False,
    "needs_marker_layer": False,
    "needs_boundary_layer": False,
    "needs_timelapse_layer": False,
    "needs_comparison_layer": False,
    "needs_proximity_layer": False,
}
