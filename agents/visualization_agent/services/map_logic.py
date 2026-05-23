"""
Visualization Agent Module 1 — Map type inference, layer flag updates, and field validation.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .constants import DEFAULT_LAYER_REQUIREMENTS, SUPPORTED_MAP_TYPES
from .helpers import ensure_dict, ensure_list


def _explicit_map_type_text(output: Dict[str, Any]) -> str:
    """Return only user-authored/request fields, avoiding repaired map fields."""
    intent = ensure_dict(output.get("structured_intent"))
    additional = ensure_dict(intent.get("additional_parameters"))
    text_parts = [
        output.get("user_query", ""),
        intent.get("requested_visualization_type", ""),
        intent.get("visualization_type", ""),
        intent.get("map_type", ""),
        additional.get("requested_visualization_type", ""),
        additional.get("visualization_type", ""),
        additional.get("map_type", ""),
    ]
    return " ".join(str(part) for part in text_parts if part not in [None, ""]).lower()


def _matches(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def detect_explicit_map_type(output: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """
    Detect map types the user explicitly named.

    This intentionally checks the raw/request text before any semantic fallback,
    so phrases like "3d heatmap" are not overwritten by density/time heuristics.
    """
    text = _explicit_map_type_text(output)
    if not text:
        return None, ""

    dimensional_3d = r"(?:3\s*[- ]?\s*d|three\s*[- ]?\s*dimensional)"
    dimensional_2d = r"(?:2\s*[- ]?\s*d|two\s*[- ]?\s*dimensional)"
    heatmap = r"heat\s*maps?"
    timelapse = r"(?:time\s*[- ]?\s*lapse|timelapse|animated\s*time|time\s*slider)"
    floor_wise = r"(?:floor\s*[- ]?\s*wise|floorwise|floor\s+level|floor\s+by\s+floor|floor\s+rates?|floor\s+comparison)"

    explicit_patterns = [
        (
            "3d_heatmap",
            [
                rf"\b{dimensional_3d}\b.{{0,40}}\b{heatmap}\b",
                rf"\b{heatmap}\b.{{0,40}}\b{dimensional_3d}\b",
            ],
            "User explicitly requested a 3D heatmap, so 3d_heatmap overrides metric/time fallback.",
        ),
        (
            "3d_timelapse",
            [
                rf"\b{dimensional_3d}\b.{{0,40}}\b{timelapse}\b",
                rf"\b{timelapse}\b.{{0,40}}\b{dimensional_3d}\b",
            ],
            "User explicitly requested a 3D timelapse/time visualization, so 3d_timelapse was selected.",
        ),
        (
            "3d_floor_wise",
            [
                rf"\b{dimensional_3d}\b.{{0,50}}\b{floor_wise}\b",
                rf"\b{floor_wise}\b.{{0,50}}\b{dimensional_3d}\b",
                r"\b3d\s*[- ]?floor\s*[- ]?wise\b",
            ],
            "User explicitly requested a 3D floor-wise visualization, so 3d_floor_wise was selected.",
        ),
        (
            "3d_building_plotting",
            [
                rf"\b{dimensional_3d}\b.{{0,40}}\b(?:building|buildings|fsi|plotting)\b",
                r"\b(?:building|buildings|fsi)\b.{0,40}\b(?:plot|plotting|map|visuali[sz]e)\b",
            ],
            "User explicitly requested 3D/building plotting, so 3d_building_plotting was selected.",
        ),
        (
            "2d_heatmap",
            [
                rf"\b{dimensional_2d}\b.{{0,40}}\b{heatmap}\b",
                rf"\b{heatmap}\b.{{0,40}}\b{dimensional_2d}\b",
                rf"\b{heatmap}\b",
            ],
            "User explicitly requested a heatmap, so 2d_heatmap was selected.",
        ),
        (
            "region_choropleth",
            [r"\bchoropleth\b", r"\bregion(?:al)?\s+(?:color|colour|shaded)\s+map\b"],
            "User explicitly requested a choropleth/regional shaded map, so region_choropleth was selected.",
        ),
        (
            "2d_overlay",
            [r"\b(?:2\s*[- ]?\s*d\s+)?overlay\s+map\b", r"\bboundary\s+overlay\b"],
            "User explicitly requested an overlay map, so 2d_overlay was selected.",
        ),
        (
            "cluster_map",
            [r"\bcluster(?:ed)?\s+map\b", r"\bmap\s+clusters?\b"],
            "User explicitly requested a cluster map, so cluster_map was selected.",
        ),
        (
            "marker_map",
            [r"\bmarker\s+map\b", r"\bpin\s+map\b", r"\bpoint\s+map\b"],
            "User explicitly requested a marker/point map, so marker_map was selected.",
        ),
        (
            "proximity_map",
            [r"\bproximity\s+map\b", r"\bnearby\s+map\b"],
            "User explicitly requested a proximity map, so proximity_map was selected.",
        ),
        (
            "comparison_map",
            [r"\bcomparison\s+map\b", r"\bcompare\s+map\b"],
            "User explicitly requested a comparison map, so comparison_map was selected.",
        ),
    ]

    for map_type, patterns, reason in explicit_patterns:
        if map_type in SUPPORTED_MAP_TYPES and any(_matches(pattern, text) for pattern in patterns):
            return map_type, reason

    return None, ""


def infer_default_map_type(output: Dict[str, Any]) -> Tuple[str, str]:
    """First-iteration map fallback selection when no explicit map type is given."""
    explicit_map_type, explicit_reason = detect_explicit_map_type(output)
    if explicit_map_type:
        return explicit_map_type, explicit_reason

    intent = output.get("structured_intent", {})
    classification = output.get("request_classification", {})

    metric_text = json.dumps(intent, ensure_ascii=False).lower()
    user_query = str(output.get("user_query", "")).lower()
    dimension_text = str(intent.get("primary_dimension", "")).lower()
    geography_level = str(intent.get("geography_level", "")).lower()
    time_dimension = str(intent.get("time_dimension", "")).lower()
    combined_text = f"{metric_text} {user_query}"

    is_simulation = bool(classification.get("is_simulation_request", False))
    is_what_if = bool(classification.get("is_what_if_request", False))
    is_spatial = bool(classification.get("is_spatial_analysis_request", False))

    if is_simulation or is_what_if or "what if" in combined_text or "impact" in combined_text:
        return "comparison_map", "Simulation/what-if or impact intent detected, so comparison_map was selected."

    if is_spatial or "near" in combined_text or "proximity" in combined_text or "metro" in combined_text:
        return "proximity_map", "Spatial/proximity intent detected, so proximity_map was selected."

    if "floor" in combined_text and ("3d" in combined_text or "building" in combined_text):
        return "3d_floor_wise", "3D floor-level intent detected, so 3d_floor_wise was selected."

    if "3d" in combined_text or "building" in combined_text or "fsi" in combined_text:
        return "3d_building_plotting", "3D/building/FSI intent detected, so 3d_building_plotting was selected."

    if "density" in combined_text or "demand" in combined_text or "sales" in combined_text or "transaction" in combined_text:
        return "2d_heatmap", "Density, demand, sales, or transaction query detected, so 2d_heatmap was selected."

    if "village" in geography_level or "micromarket" in geography_level or "region" in geography_level:
        return "2d_overlay", "Region/village/micromarket query detected, so 2d_overlay was selected."

    if "project" in geography_level or "project" in dimension_text:
        return "marker_map", "Project-level query detected, so marker_map was selected."

    if time_dimension or "year" in combined_text or "quarter" in combined_text or "month" in combined_text:
        return "2d_heatmap", "Time-based location query detected, so 2d_heatmap with time payload was selected."

    return "marker_map", "No specific visualization type was provided, so marker_map was selected as the safest default."


def update_layer_flags(map_req: Dict[str, Any]) -> Dict[str, Any]:
    layer_flags = ensure_dict(map_req.get("layer_requirements"))
    merged_flags = DEFAULT_LAYER_REQUIREMENTS.copy()
    merged_flags.update({k: bool(v) for k, v in layer_flags.items()})

    selected_map_types = ensure_list(map_req.get("selected_map_types"))
    primary_map_type = map_req.get("primary_map_type")
    if primary_map_type and primary_map_type not in selected_map_types:
        selected_map_types.insert(0, primary_map_type)

    for map_type in selected_map_types:
        if map_type in ["2d_heatmap"]:
            merged_flags["needs_2d_layer"] = True
            merged_flags["needs_heatmap_layer"] = True
        elif map_type in ["marker_map", "cluster_map"]:
            merged_flags["needs_2d_layer"] = True
            merged_flags["needs_marker_layer"] = True
        elif map_type in ["2d_overlay", "region_choropleth"]:
            merged_flags["needs_2d_layer"] = True
            merged_flags["needs_boundary_layer"] = True
        elif map_type in ["3d_building_plotting", "3d_floor_wise", "3d_heatmap", "3d_timelapse"]:
            merged_flags["needs_3d_layer"] = True
        if map_type == "3d_floor_wise":
            merged_flags["needs_marker_layer"] = True
        if map_type == "3d_heatmap":
            merged_flags["needs_heatmap_layer"] = True
        if map_type == "3d_timelapse":
            merged_flags["needs_timelapse_layer"] = True
        if map_type == "comparison_map":
            merged_flags["needs_comparison_layer"] = True
        if map_type == "proximity_map":
            merged_flags["needs_proximity_layer"] = True

    map_req["layer_requirements"] = merged_flags
    return map_req


def validate_minimum_block_fields(output: Dict[str, Any]) -> List[str]:
    """Minimum validation only. Dynamic fields are allowed and not rejected."""
    missing: List[str] = []
    map_req = output.get("map_output_requirements", {})

    minimum_map_fields = [
        "selected_map_types",
        "primary_map_type",
        "base_map_metric",
        "geo_level",
    ]
    for field in minimum_map_fields:
        value = map_req.get(field)
        if value in [None, "", []]:
            missing.append(f"map_output_requirements.{field}")

    if output.get("simulation_requirements", {}).get("is_active"):
        for field in ["scenario_variable", "change_type", "target_metric"]:
            if output["simulation_requirements"].get(field) in [None, "", []]:
                missing.append(f"simulation_requirements.{field}")

    if output.get("what_if_requirements", {}).get("is_active"):
        for field in ["base_case", "changed_case", "comparison_metric"]:
            if output["what_if_requirements"].get(field) in [None, "", []]:
                missing.append(f"what_if_requirements.{field}")

    if output.get("spatial_requirements", {}).get("is_active"):
        if output["spatial_requirements"].get("analysis_type") in [None, "", []]:
            missing.append("spatial_requirements.analysis_type")
        output["spatial_requirements"].setdefault("spatial_input_dependency", "plotted_map_output")

    return missing
