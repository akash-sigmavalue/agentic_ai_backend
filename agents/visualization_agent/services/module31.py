"""
Visualization Agent Module 3.1 - LLM-assisted dynamic map builder.

The LLM produces a planner, renderer spec/code artifact, and validation pass.
The app only executes approved renderer templates from the validated spec.
"""

import json
import time
from typing import Any, Dict, List, Tuple

from openai import OpenAI

from core.config import settings
from .constants import DEFAULT_MODEL, SUPPORTED_MAP_TYPES
from .helpers import extract_json_from_text, ensure_dict
from .openai_client import calculate_cost, extract_usage_from_response


TEMPLATE_CATALOG: Dict[str, Dict[str, Any]] = {
    "2d_overlay": {
        "family": "2d",
        "base_component": "MapOverlayView",
        "reference_logic": [
            "Leaflet base map",
            "polygon draw/delete via leaflet-draw",
            "circle radius filtering",
            "map coloring and layer toggles",
            "popup and table output",
        ],
    },
    "2d_heatmap": {
        "family": "2d",
        "base_component": "MapOverlayView",
        "reference_logic": ["circle intensity markers", "metric color scaling", "time filter when available"],
    },
    "marker_map": {
        "family": "2d",
        "base_component": "MapOverlayView",
        "reference_logic": ["Leaflet markers", "popups", "table rows"],
    },
    "cluster_map": {
        "family": "2d",
        "base_component": "MapOverlayView",
        "reference_logic": ["point aggregation", "marker density styling", "popups"],
    },
    "region_choropleth": {
        "family": "2d",
        "base_component": "MapOverlayView",
        "reference_logic": ["boundary/polygon coloring", "metric legend", "regional popups"],
    },
    "comparison_map": {
        "family": "2d",
        "base_component": "MapOverlayView",
        "reference_logic": ["side-by-side metric encoding", "difference colors", "comparison popups"],
    },
    "proximity_map": {
        "family": "spatial-analysis",
        "base_component": "SpatialAnalysisView",
        "reference_logic": ["subject point", "nearby projects/roads/places", "proximity insights"],
    },
    "3d_building_plotting": {
        "family": "3d",
        "base_component": "ThreeDMapView",
        "reference_logic": ["Overture building polygons", "deck.gl extrusion", "building popups"],
    },
    "3d_floor_wise": {
        "family": "3d",
        "base_component": "ThreeDMapTimelapseView",
        "reference_logic": [
            "Overture building polygons",
            "floor-wise extrusion bands",
            "floor metric coloring",
            "floor hover tooltip",
            "timeline playback only when Module 2 has time frames",
        ],
    },
    "3d_heatmap": {
        "family": "3d",
        "base_component": "ThreeDMapView",
        "reference_logic": [
            "fetch Overture buildings around Module 2 lat/lng vicinity",
            "deck.gl extruded building polygons",
            "heatmap color ramp from Module 2 metric",
            "metric legend and building hover tooltip",
        ],
    },
    "3d_timelapse": {
        "family": "3d-timelapse",
        "base_component": "ThreeDMapTimelapseView",
        "reference_logic": ["Overture building polygons", "floor/time slider", "animated rate coloring"],
    },
    "heatmap-timelapse": {
        "family": "heatmap-timelapse",
        "base_component": "HeatmapTimelapseView",
        "reference_logic": ["Overture buildings", "IDW heat interpolation", "timeline playback"],
    },
}

RENDERER_FAMILY_OVERRIDES: Dict[str, Tuple[str, str, str]] = {
    "marker_map": ("2d", "MapOverlayView", "marker_map"),
    "cluster_map": ("2d", "MapOverlayView", "cluster_map"),
    "2d_overlay": ("2d", "MapOverlayView", "2d_overlay"),
    "2d_heatmap": ("2d", "MapOverlayView", "2d_heatmap"),
    "region_choropleth": ("2d", "MapOverlayView", "region_choropleth"),
    "comparison_map": ("2d", "MapOverlayView", "comparison_map"),
    "proximity_map": ("spatial-analysis", "SpatialAnalysisView", "proximity_map"),
    "3d_building_plotting": ("3d", "ThreeDMapView", "3d_building_plotting"),
    "3d_floor_wise": ("3d", "ThreeDMapTimelapseView", "3d_floor_wise"),
    "3d_timelapse": ("3d-timelapse", "ThreeDMapTimelapseView", "3d_timelapse"),
}


LAT_FIELDS = ["latitude", "lat", "project_latitude", "subject_lat", "center_lat", "y"]
LNG_FIELDS = ["longitude", "lng", "lon", "long", "project_longitude", "subject_lon", "subject_lng", "center_lng", "x"]
TIME_FIELDS = ["year", "month", "date", "period", "time_period", "transaction_year", "transaction_date"]
LABEL_FIELDS = ["project_name", "location_name", "location", "locality", "village", "village_name", "micromarket", "city"]


def _records_from_module2(module2_output: Dict[str, Any]) -> List[Dict[str, Any]]:
    visualization_output = ensure_dict(module2_output.get("visualization_ready_output"))
    records = visualization_output.get("records")
    if isinstance(records, list):
        return [row for row in records if isinstance(row, dict)]

    dataset = module2_output.get("analysis_ready_dataset")
    if isinstance(dataset, list):
        return [row for row in dataset if isinstance(row, dict)]

    return []


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _find_field(record: Dict[str, Any], candidates: List[str]) -> str | None:
    normalized = {_normalize_key(key): key for key in record.keys()}
    for candidate in candidates:
        key = normalized.get(_normalize_key(candidate))
        if key:
            return key
    return None


def _to_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _infer_field_mapping(module1_output: Dict[str, Any], module2_output: Dict[str, Any]) -> Dict[str, Any]:
    records = _records_from_module2(module2_output)
    if not records:
        return {}

    first = records[0]
    mapped = ensure_dict(module2_output.get("mapped_fields"))
    map_req = ensure_dict(module1_output.get("map_output_requirements"))
    readiness = ensure_dict(module2_output.get("map_readiness"))
    lat_field = _find_field(first, [str(mapped.get("latitude_field", "")), *LAT_FIELDS])
    lng_field = _find_field(first, [str(mapped.get("longitude_field", "")), *LNG_FIELDS])
    label_field = _find_field(first, [str(mapped.get("geo_field", "")), *LABEL_FIELDS])
    time_field = _find_field(first, [str(readiness.get("time_field", "")), str(mapped.get("time_field", "")), *TIME_FIELDS])

    metric_candidates = [
        str(readiness.get("intensity_field", "")),
        str(mapped.get("metric_field", "")),
        str(map_req.get("intensity_metric", "")),
        str(map_req.get("base_map_metric", "")),
        "metric_value",
        "sales_density",
        "density",
        "transaction_count",
        "sales_count",
        "avg_rate_psf",
        "rate_per_sq_ft",
    ]
    metric_field = _find_field(first, metric_candidates)
    if not metric_field or _to_number(first.get(metric_field)) is None:
        blocked = {_normalize_key(value) for value in [*LAT_FIELDS, *LNG_FIELDS, *TIME_FIELDS]}
        for key, value in first.items():
            if _normalize_key(key) not in blocked and _to_number(value) is not None:
                metric_field = key
                break

    return {
        "latitude_field": lat_field,
        "longitude_field": lng_field,
        "metric_field": metric_field,
        "label_field": label_field,
        "time_field": time_field,
        "record_count": len(records),
    }


def _summarize_inputs(module1_output: Dict[str, Any], module2_output: Dict[str, Any]) -> Dict[str, Any]:
    records = _records_from_module2(module2_output)
    sample_records = records[:12]
    columns = sorted({key for row in sample_records for key in row.keys()})
    map_req = ensure_dict(module1_output.get("map_output_requirements"))
    spatial_req = ensure_dict(module1_output.get("spatial_requirements"))
    primary_map_type = str(map_req.get("primary_map_type") or "marker_map")
    if primary_map_type == "3d_heatmap" and map_req.get("timelapse_required"):
        template_key = "heatmap-timelapse"
    elif primary_map_type == "3d_timelapse":
        template_key = "3d_timelapse"
    elif primary_map_type == "3d_floor_wise":
        template_key = "3d_floor_wise"
    elif primary_map_type == "proximity_map" or spatial_req.get("is_active"):
        template_key = "proximity_map"
    else:
        template_key = primary_map_type

    return {
        "business_objective": module1_output.get("business_objective"),
        "primary_map_type": primary_map_type,
        "selected_map_types": map_req.get("selected_map_types", []),
        "timelapse_required": bool(map_req.get("timelapse_required")),
        "spatial_requirements": spatial_req,
        "module2_status": module2_output.get("status"),
        "module2_next_ready": module2_output.get("next_module_ready"),
        "module2_map_readiness": module2_output.get("map_readiness", {}),
        "columns": columns,
        "sample_records": sample_records,
        "inferred_field_mapping": _infer_field_mapping(module1_output, module2_output),
        "template_catalog": TEMPLATE_CATALOG,
        "recommended_template_key": template_key if template_key in TEMPLATE_CATALOG else primary_map_type,
    }


def _response_call(
    client: OpenAI,
    *,
    model: str,
    system_prompt: str,
    user_payload: Dict[str, Any],
    call_name: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
        ],
    )
    usage = extract_usage_from_response(response)
    cost = calculate_cost(
        model,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cached_input_tokens=usage["cached_input_tokens"],
    )
    return extract_json_from_text(response.output_text), {
        "call_name": call_name,
        "model": model,
        **usage,
        **cost,
    }


def _planner_prompt() -> str:
    return """
You are Module 3.1 Step 1: Map Implementation Planner.
Return only JSON.
Choose the best base template from template_catalog for the primary map type and data.
Explicit user primary_map_type wins. Do not downgrade 3d_heatmap to 2d_heatmap.
If primary_map_type is 3d_heatmap and timelapse_required is true, choose base_template_key "heatmap-timelapse" and selected_family "heatmap-timelapse".
If primary_map_type is 2d_heatmap, choose base_template_key "2d_heatmap" and selected_family "2d" even when time-aware fields exist.
If primary_map_type is 3d_floor_wise, choose base_template_key "3d_floor_wise" and selected_family "3d"; use the floor-wise 3D template, not the generic heatmap template.
If primary_map_type is 3d_timelapse, choose base_template_key "3d_timelapse" and selected_family "3d-timelapse".
If spatial_requirements.is_active is true or primary_map_type is proximity_map, choose base_template_key "proximity_map" and selected_family "spatial-analysis".
For 3d_heatmap, plan to load Overture buildings around the Module 2 lat/lng vicinity and apply heatmap metric coloring to buildings.
Required JSON keys:
{
  "step": "planner",
  "selected_map_type": "...",
  "selected_family": "2d|3d|3d-timelapse|spatial-analysis|heatmap-timelapse",
  "base_template_key": "...",
  "base_component": "...",
  "field_mapping": {},
  "required_layers": [],
  "required_controls": [],
  "external_data_requirements": [],
  "rendering_strategy": "...",
  "cache_key_hint": "...",
  "risks": []
}
"""


def _renderer_prompt() -> str:
    return """
You are Module 3.1 Step 2: Renderer Spec and Code Artifact Generator.
Return only JSON.
Use the selected base template and write a constrained implementation artifact for the dynamic map.
Do not import unknown packages. Do not generate network calls except approved template APIs:
- Overture building loading through existing 3D/heatmap template APIs.
- Existing Leaflet/deck.gl sample map logic.
Required JSON keys:
{
  "step": "renderer_generator",
  "renderer_spec": {
    "family": "...",
    "renderer": "...",
    "base_component": "...",
    "field_mapping": {},
    "layers": [],
    "controls": [],
    "visual_encoding": {
      "color_palette": ["#2563eb", "#0891b2", "#16a34a", "#ca8a04", "#f97316", "#dc2626"],
      "threshold_strategy": "linear",
      "geometry_type": "point",
      "radius_range": {"min": 7, "max": 20},
      "line_weight_range": {"min": 3, "max": 10},
      "legend_labels": {"low": "Low", "mid": "Medium", "high": "High"}
    },
    "data_pipeline": [],
    "overture": {"required": false, "radius_m": 500, "center_strategy": "mean_lat_lng"},
    "interaction_model": {},
    "template_bindings": {}
  },
  "generated_code_artifact": {
    "language": "tsx",
    "component_name": "GeneratedModule31Map",
    "code": "...",
    "notes": []
  }
}
"""


def _validator_prompt() -> str:
    return """
You are Module 3.1 Step 3: Validation and Repair.
Return only JSON.
Validate that the generated renderer spec is compatible with the approved template catalog and Module 2 fields.
Repair only inside the JSON spec. Do not change explicit 3d_heatmap to 2d_heatmap.
For 3d_heatmap with timelapse_required true, ensure family is "heatmap-timelapse" and base_component is "HeatmapTimelapseView".
For 3d_heatmap without timelapse_required, ensure overture.required is true and family is "3d".
For 2d_heatmap, ensure family is "2d" and base_component is "MapOverlayView"; do not route it to HeatmapTimelapseView.
For all generated 2D maps, visual_encoding must define color_palette, threshold_strategy, geometry_type, radius_range, line_weight_range, and legend_labels. Do not use opacity as a metric encoding.
Use geometry_type by renderer: marker_map/cluster_map/comparison_map/2d_heatmap usually "point"; road/metro/highway overlays "line"; catchment/radius overlays "circle"; region_choropleth "polygon".
For 3d_floor_wise, ensure family is "3d", base_component is "ThreeDMapTimelapseView", and template_key is "3d_floor_wise".
For 3d_timelapse, ensure family is "3d-timelapse" and base_component is "ThreeDMapTimelapseView".
For proximity_map or active spatial requirements, ensure family is "spatial-analysis" and base_component is "SpatialAnalysisView".
Required JSON keys:
{
  "step": "validator",
  "is_valid": true,
  "repairs_applied": [],
  "final_renderer_spec": {},
  "final_generated_code_artifact": {},
  "execution_policy": {
    "execute_generated_code": false,
    "approved_runtime_renderer": "...",
    "reason": "..."
  },
  "warnings": []
}
"""


def _normalize_final_spec(summary: Dict[str, Any], planner: Dict[str, Any], renderer: Dict[str, Any], validator: Dict[str, Any]) -> Dict[str, Any]:
    final_spec = ensure_dict(validator.get("final_renderer_spec")) or ensure_dict(renderer.get("renderer_spec"))
    field_mapping = ensure_dict(final_spec.get("field_mapping"))
    inferred = ensure_dict(summary.get("inferred_field_mapping"))
    for key, value in inferred.items():
        field_mapping.setdefault(key, value)
    final_spec["field_mapping"] = field_mapping

    selected_map_type = str(planner.get("selected_map_type") or summary.get("primary_map_type") or "marker_map")
    if selected_map_type not in SUPPORTED_MAP_TYPES:
        selected_map_type = str(summary.get("primary_map_type") or "marker_map")
    final_spec["renderer"] = selected_map_type

    timelapse_required = bool(summary.get("timelapse_required"))
    if selected_map_type == "3d_heatmap" and timelapse_required:
        template_key = "heatmap-timelapse"
    elif selected_map_type in RENDERER_FAMILY_OVERRIDES:
        template_key = selected_map_type
    else:
        template_key = str(planner.get("base_template_key") or summary.get("recommended_template_key") or selected_map_type)
    template = TEMPLATE_CATALOG.get(template_key) or TEMPLATE_CATALOG.get(selected_map_type) or TEMPLATE_CATALOG["marker_map"]
    final_spec["family"] = template["family"]
    final_spec["base_component"] = template["base_component"]
    final_spec["template_key"] = template_key
    visual_encoding = ensure_dict(final_spec.get("visual_encoding"))
    palette = visual_encoding.get("color_palette") or visual_encoding.get("palette") or visual_encoding.get("colors")
    if not isinstance(palette, list) or len(palette) < 2:
        palette = ["#2563eb", "#0891b2", "#16a34a", "#ca8a04", "#f97316", "#dc2626"]
    radius_range = ensure_dict(visual_encoding.get("radius_range") or visual_encoding.get("marker_radius"))
    min_radius = radius_range.get("min", visual_encoding.get("min_radius", 7))
    max_radius = radius_range.get("max", visual_encoding.get("max_radius", 20))
    try:
        min_radius_number = max(3, min(24, float(min_radius)))
    except (TypeError, ValueError):
        min_radius_number = 7
    try:
        max_radius_number = max(6, min(36, float(max_radius)))
    except (TypeError, ValueError):
        max_radius_number = 20
    if max_radius_number <= min_radius_number:
        max_radius_number = min_radius_number + 6
    threshold_strategy = str(visual_encoding.get("threshold_strategy") or "linear").lower()
    if threshold_strategy not in {"linear", "quantile"}:
        threshold_strategy = "linear"
    geometry_type = str(visual_encoding.get("geometry_type") or visual_encoding.get("geometry") or "point").lower()
    if geometry_type not in {"point", "circle", "line", "polygon"}:
        geometry_type = "point"
    line_weight_range = ensure_dict(visual_encoding.get("line_weight_range") or visual_encoding.get("stroke_weight"))
    min_line_weight = line_weight_range.get("min", visual_encoding.get("min_line_weight", 3))
    max_line_weight = line_weight_range.get("max", visual_encoding.get("max_line_weight", 10))
    try:
        min_line_weight_number = max(1, min(12, float(min_line_weight)))
    except (TypeError, ValueError):
        min_line_weight_number = 3
    try:
        max_line_weight_number = max(2, min(20, float(max_line_weight)))
    except (TypeError, ValueError):
        max_line_weight_number = 10
    if max_line_weight_number <= min_line_weight_number:
        max_line_weight_number = min_line_weight_number + 2
    legend_labels = ensure_dict(visual_encoding.get("legend_labels"))
    final_spec["visual_encoding"] = {
        "color_palette": [str(color) for color in palette[:7]],
        "threshold_strategy": threshold_strategy,
        "geometry_type": geometry_type,
        "radius_range": {"min": min_radius_number, "max": max_radius_number},
        "line_weight_range": {"min": min_line_weight_number, "max": max_line_weight_number},
        "legend_labels": {
            "low": str(legend_labels.get("low") or "Low"),
            "mid": str(legend_labels.get("mid") or "Medium"),
            "high": str(legend_labels.get("high") or "High"),
        },
    }
    if selected_map_type in {"marker_map", "cluster_map", "2d_heatmap", "comparison_map"}:
        final_spec["visual_encoding"]["geometry_type"] = "point"
    elif selected_map_type == "region_choropleth":
        final_spec["visual_encoding"]["geometry_type"] = "polygon"

    if selected_map_type == "3d_heatmap":
        if timelapse_required:
            final_spec["family"] = "heatmap-timelapse"
            final_spec["base_component"] = "HeatmapTimelapseView"
            final_spec["template_key"] = "heatmap-timelapse"
        else:
            final_spec["family"] = "3d"
            final_spec["base_component"] = "ThreeDMapView"
        overture = ensure_dict(final_spec.get("overture"))
        overture["required"] = True
        overture.setdefault("radius_m", 500)
        overture.setdefault("center_strategy", "mean_lat_lng")
        final_spec["overture"] = overture

    if selected_map_type in RENDERER_FAMILY_OVERRIDES:
        family, base_component, forced_template_key = RENDERER_FAMILY_OVERRIDES[selected_map_type]
        final_spec["family"] = family
        final_spec["base_component"] = base_component
        final_spec["template_key"] = forced_template_key

    if selected_map_type == "3d_floor_wise":
        overture = ensure_dict(final_spec.get("overture"))
        overture["required"] = True
        overture.setdefault("radius_m", 450)
        overture.setdefault("center_strategy", "module2_focus_points")
        final_spec["overture"] = overture

    if selected_map_type == "3d_timelapse":
        final_spec["family"] = "3d-timelapse"
        final_spec["base_component"] = "ThreeDMapTimelapseView"
        final_spec["template_key"] = "3d_timelapse"
        overture = ensure_dict(final_spec.get("overture"))
        overture["required"] = True
        overture.setdefault("radius_m", 500)
        overture.setdefault("center_strategy", "mean_lat_lng")
        final_spec["overture"] = overture

    spatial_req = ensure_dict(summary.get("spatial_requirements"))
    if selected_map_type == "proximity_map" or spatial_req.get("is_active"):
        final_spec["family"] = "spatial-analysis"
        final_spec["base_component"] = "SpatialAnalysisView"
        final_spec["template_key"] = "proximity_map"

    return final_spec


def generate_module31_map(
    *,
    module1_output: Dict[str, Any],
    module2_output: Dict[str, Any],
    model: str = DEFAULT_MODEL,
) -> Dict[str, Any]:
    api_key = settings.OPENAI_API_KEY or ""
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not configured in the backend .env file.")

    start_time = time.time()
    client = OpenAI(api_key=api_key)
    summary = _summarize_inputs(module1_output, module2_output)

    planner, usage1 = _response_call(
        client,
        model=model,
        system_prompt=_planner_prompt(),
        user_payload={"module31_input_summary": summary},
        call_name="module31_planner",
    )
    renderer, usage2 = _response_call(
        client,
        model=model,
        system_prompt=_renderer_prompt(),
        user_payload={"module31_input_summary": summary, "planner_output": planner},
        call_name="module31_renderer_generator",
    )
    validator, usage3 = _response_call(
        client,
        model=model,
        system_prompt=_validator_prompt(),
        user_payload={
            "module31_input_summary": summary,
            "planner_output": planner,
            "renderer_output": renderer,
            "template_catalog": TEMPLATE_CATALOG,
        },
        call_name="module31_validator",
    )

    ledger = [usage1, usage2, usage3]
    totals = {
        "total_llm_calls": 3,
        "total_input_tokens": sum(row.get("input_tokens", 0) for row in ledger),
        "total_cached_input_tokens": sum(row.get("cached_input_tokens", 0) for row in ledger),
        "total_output_tokens": sum(row.get("output_tokens", 0) for row in ledger),
        "total_tokens": sum(row.get("total_tokens", 0) for row in ledger),
        "total_cost_usd": round(sum(row.get("total_cost", 0.0) for row in ledger), 8),
        "ledger": ledger,
    }
    final_spec = _normalize_final_spec(summary, planner, renderer, validator)
    code_artifact = ensure_dict(validator.get("final_generated_code_artifact")) or ensure_dict(renderer.get("generated_code_artifact"))

    return {
        "module_number": 3.1,
        "module_name": "Dynamic Map Builder",
        "status": "success",
        "llm_call_count": 3,
        "processing_time_seconds": round(time.time() - start_time, 2),
        "input_summary": summary,
        "planner_output": planner,
        "renderer_output": renderer,
        "validator_output": validator,
        "final_renderer_spec": final_spec,
        "generated_code_artifact": code_artifact,
        "usage": totals,
        "cache_policy": "frontend_state_until_page_reload",
    }
