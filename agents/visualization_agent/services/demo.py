"""
Visualization Agent Module 1 — Demo fallback output for testing without API key.
"""

from typing import Any, Dict

from .constants import MODULE_NAMES
from .execution_plan import build_default_execution_plan
from .repair import validate_and_repair_output


def demo_intent_output(user_query: str) -> Dict[str, Any]:
    """Local fallback output for UI review before API key is added."""
    output = {
        "module_number": 1,
        "module_name": MODULE_NAMES["module_1"],
        "module_purpose": "Understand the user query and decide what the Visualization Agent must do.",
        "user_query": user_query,
        "business_objective": "Analyze the requested real estate metric and prepare the most suitable map-based visualization workflow.",
        "structured_intent": {
            "primary_metric": "sales",
            "metric_type": "density",
            "metric_aggregation": "count",
            "primary_dimension": "location",
            "secondary_dimension": "project",
            "geography_level": "project_or_locality",
            "time_dimension": "year",
            "time_range": {},
            "filters": {},
            "output_expectation": "map_based_visualization",
        },
        "request_classification": {
            "request_type": "map_based_spatial_analysis",
            "is_plotting_request": True,
            "is_simulation_request": False,
            "is_what_if_request": False,
            "is_spatial_analysis_request": True,
            "is_insight_only_request": False,
        },
        "map_output_requirements": {
            "is_active": True,
            "is_map_output_required": True,
            "visualization_intent_provided": False,
            "visualization_auto_selected": True,
            "auto_selected_reason": "Demo fallback selected 2d_heatmap as a safe default for location-based analysis.",
            "selected_map_types": ["2d_heatmap", "2d_overlay"],
            "primary_map_type": "2d_heatmap",
            "secondary_map_types": ["2d_overlay"],
            "base_map_metric": "sales",
            "map_metric_aggregation": "count",
            "intensity_metric": "sales_density",
            "geo_level": "project_or_locality",
            "location_fields_required": ["latitude", "longitude", "project_name", "location_name"],
            "optional_location_fields": ["village", "micromarket", "city"],
            "time_field_required": True,
            "time_granularity": "annual",
            "timelapse_required": True,
            "timelapse_mode": "time_slider",
            "tooltip_fields": ["project_name", "location_name", "sales_count", "year"],
            "layer_requirements": {
                "needs_2d_layer": True,
                "needs_3d_layer": False,
                "needs_heatmap_layer": True,
                "needs_marker_layer": False,
                "needs_boundary_layer": True,
                "needs_timelapse_layer": True,
                "needs_comparison_layer": False,
                "needs_proximity_layer": False,
            },
            "additional_parameters": {
                "heatmap_intensity_basis": "sales_density",
                "fallback_boundary_layer": "location_or_micromarket_if_available",
                "time_aware_map": True,
                "timelapse_payload_required": True,
                "timelapse_mode": "time_slider",
            },
        },
        "simulation_requirements": {"is_active": False, "additional_parameters": {}},
        "what_if_requirements": {"is_active": False, "additional_parameters": {}},
        "spatial_requirements": {
            "is_active": True,
            "analysis_type": ["hotspot_detection", "density_pattern_analysis", "region_comparison"],
            "spatial_objective": "Identify high-performing areas using plotted map output.",
            "contextual_layers_required": False,
            "context_layers": [],
            "spatial_input_dependency": "plotted_map_output",
            "expected_spatial_output": [
                "high_density_locations",
                "low_density_locations",
                "location_wise_performance_pattern",
            ],
            "additional_parameters": {},
        },
        "insight_requirements": {
            "is_active": True,
            "required_insight_type": [
                "density_summary",
                "high_performing_area_identification",
                "business_recommendation",
            ],
            "insight_style": "business_summary",
            "evidence_required": True,
            "additional_parameters": {},
        },
    }
    output["execution_plan"] = build_default_execution_plan(output)
    return validate_and_repair_output(output, user_query)
