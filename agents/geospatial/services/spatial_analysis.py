"""
Spatial Analysis Service — ported from mapapi.py
Removes Streamlit / Folium; returns structured data for the frontend to render.
"""
from __future__ import annotations

import json
import math
import time
import functools
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from openai import OpenAI

from core.config import settings


# =========================================================
# CONFIG
# =========================================================

MODEL_NAME = "gpt-4o-mini"

PRICE_INPUT_PER_1M = 0.15
PRICE_OUTPUT_PER_1M = 0.60

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "SigmaValueSpatialPilot/1.0"

DEFAULT_QUERY = (
    "Analyze the relationship between road proximity and project rates for the plotted projects. "
    "Use the Excel data and extract only the required data from OSM map context. "
    "Make a step-by-step implementation plan, run the calculations, update the map overlay, "
    "and generate insights."
)

CURRENT_PROMPT = """
You are a Spatial Intelligence and Real Estate Analysis Assistant.

Your job is to analyze user requests using:
1. structured Excel data as the main source of truth, and
2. extracted map data as contextual spatial enrichment.

You must not rely on image interpretation.
You must rely on:
- Excel/project dataset
- plotted map data
- extracted map context such as roads, amenities, infrastructure, and nearby spatial relationships

==================================================
PRIMARY OBJECTIVE
==================================================

For every relevant user request, you must:
1. analyze the impact factor,
2. define X and Y variables,
3. calculate X and other required variables,
4. represent the result on the map,
5. generate insights,
6. display all important calculations and results in tabular format.

==================================================
MANDATORY WORKFLOW
==================================================

Step 1: Analyze the impact factor relevant to the request
- Understand the user's objective.
- If the request is about **main road** impact, identify **distance to nearest main road** as the factor.
- If the request is about general road proximity, use **distance to any road**.

Step 2: Define X and Y variables
- Define the main analytical variables.
- For main-road analysis:
  - X = distance from nearest **main road** (motorway, trunk, primary, secondary, and their links)
  - Y = project rate

Step 3: Calculate the required variable(s)
- Calculate X using Excel data and extracted map data.
- Use Excel data as the primary project source.
- Use extracted map data only as contextual enrichment.
- Each project must have its own calculated X value wherever possible.

Step 4: Represent the result on the map
- Plot project data on the map.
- Overlay the analytical output, not just raw input points.
- The displayed map must reflect the calculated result.
- Do not modify the original Excel source file.

Step 5: Generate insights
- Generate insights only after the calculations are completed.
- Use computed values and plotted output.

==================================================
MANDATORY OUTPUT RULES
==================================================

Whenever the user asks for road impact, X-variable analysis, or similar spatial pricing analysis, the response must include:

1. Variable Definition
2. Calculation Table
3. Road Impact Table
4. Insights

==================================================
SECTION RULES
==================================================

SECTION 1: Variable Definition
- Clearly define X and Y.

SECTION 2: Calculation Table
- Include: Project Name, Rate, Distance from Main Road (m), Zone

SECTION 3: Road Impact Table
- Include: Project Name, Distance from Main Road, Rate, Road Impact Interpretation

SECTION 4: Insights
- Use headings: Introduction, Analysis, Conclusion

==================================================
SUBJECT PROJECT HANDLING
==================================================

If a subject project is provided:
- Its distance to nearest main road MUST be calculated.
- Its rate MUST be estimated using nearby comparable Excel projects, taking into account main-road distance similarity.
- The estimated rate and comparables MUST be clearly stated.
- The subject project MUST be highlighted on the map with a star marker.
"""

ZONE_THRESHOLDS_METERS = {
    "Premium": 50,
    "High Value Residential": 150,
    "Balanced": 300,
    "Discount": 10**9,
}

MAIN_ROAD_TYPES = {
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link"
}


# =========================================================
# OPENAI HELPERS
# =========================================================

def _get_client() -> OpenAI:
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not configured in settings.")
    return OpenAI(api_key=api_key)


def _get_usage(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or (input_tokens + output_tokens))
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def _cost_from_usage(usage: Dict[str, int]) -> float:
    return (
        usage["input_tokens"] * (PRICE_INPUT_PER_1M / 1_000_000)
        + usage["output_tokens"] * (PRICE_OUTPUT_PER_1M / 1_000_000)
    )


def _safe_json_load(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def llm_call_json(
    step_name: str,
    user_prompt: str,
    token_log: List[Dict[str, Any]],
) -> Dict[str, Any]:
    client = _get_client()
    response = client.responses.create(
        model=MODEL_NAME,
        instructions=CURRENT_PROMPT,
        input=user_prompt,
    )
    usage = _get_usage(response)
    token_log.append({
        "step": step_name,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": round(_cost_from_usage(usage), 6),
    })
    return _safe_json_load(response.output_text)


def llm_call_text(
    step_name: str,
    user_prompt: str,
    token_log: List[Dict[str, Any]],
) -> str:
    client = _get_client()
    response = client.responses.create(
        model=MODEL_NAME,
        instructions=CURRENT_PROMPT,
        input=user_prompt,
    )
    usage = _get_usage(response)
    token_log.append({
        "step": step_name,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": round(_cost_from_usage(usage), 6),
    })
    return response.output_text


# =========================================================
# DATA LOADING
# =========================================================

# =========================================================
# EXCEL + MAPPING CACHE  (avoids re-reading on every request)
# =========================================================

_excel_df_cache: Dict[str, Any] = {}   # key → (mtime, df, mapping)

def load_transaction_data(file_path: str, mapping_path: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    import os
    mtime = os.path.getmtime(file_path)
    cached = _excel_df_cache.get(file_path)
    if cached and cached["mtime"] == mtime:
        return cached["df"].copy(), cached["mapping"]

    df = pd.read_excel(file_path)
    with open(mapping_path, 'r', encoding='utf-8') as f:
        full_mapping = json.load(f)
    summary_mapping = {k: v.get("meaning", "") for k, v in full_mapping.items()}

    _excel_df_cache[file_path] = {"mtime": mtime, "df": df, "mapping": summary_mapping}
    return df.copy(), summary_mapping

def apply_data_filter(df: pd.DataFrame, logic: Dict[str, Any], progress_log: List[str]) -> pd.DataFrame:
    out = df.copy()
    filters = logic.get("filters", [])
    for f in filters:
        col = f.get("column")
        op = f.get("operator")
        val = f.get("value")
        if col in out.columns:
            try:
                if op == "==": out = out[out[col] == val]
                elif op == "!=": out = out[out[col] != val]
                elif op == "contains": out = out[out[col].astype(str).str.contains(str(val), case=False, na=False)]
                elif op == ">": out = out[out[col] > float(val)]
                elif op == "<": out = out[out[col] < float(val)]
                elif op == ">=": out = out[out[col] >= float(val)]
                elif op == "<=": out = out[out[col] <= float(val)]
                progress_log.append(f"Applied filter: {col} {op} {val}. Remaining rows: {len(out)}.")
            except Exception as e:
                progress_log.append(f"Failed to apply filter {col} {op} {val}: {e}")
            
    num = logic.get("rate_numerator", "agreement_price")
    den = logic.get("rate_denominator", "net_carpet_area_sq_m")
    if num in out.columns and den in out.columns:
        out["_calc_rate"] = pd.to_numeric(out[num], errors='coerce') / pd.to_numeric(out[den], errors='coerce')
        out = out.replace([float('inf'), -float('inf')], float('nan')).dropna(subset=["_calc_rate"])
        progress_log.append(f"Calculated base rate = {num} / {den}. Valid rows: {len(out)}.")
    elif "price_per_sq_ft_gross_carpet" in out.columns:
        out["_calc_rate"] = pd.to_numeric(out["price_per_sq_ft_gross_carpet"], errors='coerce')
        out = out.dropna(subset=["_calc_rate"])
        progress_log.append("Using price_per_sq_ft_gross_carpet as base rate.")
    else:
        out["_calc_rate"] = 0
        progress_log.append("Missing rate columns, defaulting rate to 0.")

    grp = logic.get("groupby", ["project_name", "project_latitude", "project_longitude"])
    grp = [c for c in grp if c in out.columns]
    for required_col in ["project_latitude", "project_longitude"]:
        if required_col in out.columns and required_col not in grp:
            grp.append(required_col)
            
    if not grp: grp = ["project_name", "project_latitude", "project_longitude"]
        
    agg = logic.get("aggregation_method", "mean")
    agg_fn = "median" if agg == "median" else "mean"
        
    grouped = out.groupby(grp, as_index=False)["_calc_rate"].agg(agg_fn)
    rename_cols = {"_calc_rate": "rate"}
    if "project_latitude" in grouped.columns: rename_cols["project_latitude"] = "lat"
    if "project_longitude" in grouped.columns: rename_cols["project_longitude"] = "long"
    grouped = grouped.rename(columns=rename_cols)
    
    for c in ["project_name", "lat", "long", "rate"]:
        if c not in grouped.columns:
            grouped[c] = float('nan') if c in ["lat", "long", "rate"] else "Unknown"
            
    grouped = grouped.dropna(subset=["lat", "long"])
            
    progress_log.append(f"Aggregated data to {len(grouped)} project-level entries using {agg_fn} rate.")
    return grouped



# =========================================================
# GEO HELPERS
# =========================================================

def latlon_to_local_xy_m(lat: float, lon: float, ref_lat: float, ref_lon: float) -> Tuple[float, float]:
    r = 6371000
    x = math.radians(lon - ref_lon) * r * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * r
    return x, y


def segment_distance_and_projection(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> Tuple[float, float, float]:
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2), x1, y1
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    dist = math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)
    return dist, proj_x, proj_y


def point_to_polyline_distance_m(
    point_lat: float,
    point_lon: float,
    polyline_latlon: List[Tuple[float, float]],
    ref_lat: float,
    ref_lon: float,
) -> Tuple[Optional[float], Optional[Tuple[float, float]]]:
    px, py = latlon_to_local_xy_m(point_lat, point_lon, ref_lat, ref_lon)
    best_dist = float("inf")
    best_proj_xy: Optional[Tuple[float, float]] = None

    for i in range(len(polyline_latlon) - 1):
        lat1, lon1 = polyline_latlon[i]
        lat2, lon2 = polyline_latlon[i + 1]
        x1, y1 = latlon_to_local_xy_m(lat1, lon1, ref_lat, ref_lon)
        x2, y2 = latlon_to_local_xy_m(lat2, lon2, ref_lat, ref_lon)
        dist, proj_x, proj_y = segment_distance_and_projection(px, py, x1, y1, x2, y2)
        if dist < best_dist:
            best_dist = dist
            best_proj_xy = (proj_x, proj_y)

    if best_proj_xy is None:
        return None, None

    proj_lon = ref_lon + math.degrees(best_proj_xy[0] / (6371000 * math.cos(math.radians(ref_lat))))
    proj_lat = ref_lat + math.degrees(best_proj_xy[1] / 6371000)
    return best_dist, (proj_lat, proj_lon)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def classify_zone(distance_m: float) -> str:
    if distance_m <= ZONE_THRESHOLDS_METERS["Premium"]:
        return "Premium"
    if distance_m <= ZONE_THRESHOLDS_METERS["High Value Residential"]:
        return "High Value Residential"
    if distance_m <= ZONE_THRESHOLDS_METERS["Balanced"]:
        return "Balanced"
    return "Discount"


# =========================================================
# OSM / OVERPASS
# =========================================================

def bbox_from_projects(
    df: pd.DataFrame,
    subject_lat: Optional[float] = None,
    subject_lon: Optional[float] = None,
    buffer_deg: float = 0.02,
) -> Tuple[float, float, float, float]:
    min_lat = df["lat"].min()
    max_lat = df["lat"].max()
    min_lon = df["long"].min()
    max_lon = df["long"].max()
    if subject_lat is not None and subject_lon is not None:
        min_lat = min(min_lat, subject_lat)
        max_lat = max(max_lat, subject_lat)
        min_lon = min(min_lon, subject_lon)
        max_lon = max(max_lon, subject_lon)
    return min_lat - buffer_deg, min_lon - buffer_deg, max_lat + buffer_deg, max_lon + buffer_deg


def build_overpass_query(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
    need_amenities: bool = True, need_infra: bool = True
) -> str:
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    highway_block = f"""
    way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service|unclassified|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"]({bbox});
    """
    amenity_block = ""
    if need_amenities:
        amenity_block = f"""
        node["amenity"~"school|hospital|college|university|bank|pharmacy|marketplace|clinic"]({bbox});
        way["amenity"~"school|hospital|college|university|bank|pharmacy|marketplace|clinic"]({bbox});
        relation["amenity"~"school|hospital|college|university|bank|pharmacy|marketplace|clinic"]({bbox});
        """
    infra_block = ""
    if need_infra:
        infra_block = f"""
        node["railway"="station"]({bbox});
        way["railway"="station"]({bbox});
        relation["railway"="station"]({bbox});
        node["highway"="bus_stop"]({bbox});
        node["public_transport"="station"]({bbox});
        node["amenity"="bus_station"]({bbox});
        way["amenity"="bus_station"]({bbox});
        """
    return f"""
    [out:json][timeout:60];
    (
      {highway_block}
      {amenity_block}
      {infra_block}
    );
    out body;
    >;
    out skel qt;
    """


# Simple in-memory cache keyed by bbox + flags (replaces @st.cache_data)
_osm_cache: Dict[str, Any] = {}
_osm_cache_time: Dict[str, float] = {}
_OSM_CACHE_TTL = 3600  # 1 hour


def fetch_osm_bulk(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
    need_amenities: bool = True, need_infra: bool = True
) -> Dict[str, Any]:
    cache_key = f"{min_lat:.5f},{min_lon:.5f},{max_lat:.5f},{max_lon:.5f},{need_amenities},{need_infra}"
    now = time.time()
    if cache_key in _osm_cache and (now - _osm_cache_time.get(cache_key, 0)) < _OSM_CACHE_TTL:
        return _osm_cache[cache_key]

    query = build_overpass_query(min_lat, min_lon, max_lat, max_lon, need_amenities, need_infra)
    headers = {"User-Agent": USER_AGENT}
    max_retries = 3
    sleep_seconds = [2, 5, 10]

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                OVERPASS_URL,
                data=query.encode("utf-8"),
                headers=headers,
                timeout=90,
            )
            if resp.status_code == 200:
                result = resp.json()
                _osm_cache[cache_key] = result
                _osm_cache_time[cache_key] = now
                return result
            if attempt < max_retries - 1:
                time.sleep(sleep_seconds[attempt])
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(sleep_seconds[attempt])

    raise RuntimeError("Overpass API request failed after retries.")


def _is_place_of_interest(tags: Dict[str, str]) -> bool:
    if "amenity" in tags:
        return True
    if tags.get("railway") == "station":
        return True
    if tags.get("highway") == "bus_stop":
        return True
    if tags.get("public_transport") == "station":
        return True
    return False


def _create_place_entry(
    element: Dict[str, Any], tags: Dict[str, str], lat: float, lon: float
) -> Dict[str, Any]:
    return {
        "id": element.get("id"),
        "name": tags.get("name", "Unnamed Place"),
        "lat": lat,
        "lon": lon,
        "amenity": tags.get("amenity"),
        "railway": tags.get("railway"),
        "highway": tags.get("highway"),
        "public_transport": tags.get("public_transport"),
    }


def parse_osm_payload(payload: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    elements = payload.get("elements", [])
    nodes: Dict[int, Tuple[float, float]] = {}
    ways: List[Dict[str, Any]] = []

    for el in elements:
        etype = el.get("type")
        if etype == "node" and "lat" in el and "lon" in el:
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif etype == "way":
            ways.append(el)

    def get_way_geometry(way: Dict[str, Any]) -> List[Tuple[float, float]]:
        return [nodes[nid] for nid in way.get("nodes", []) if nid in nodes]

    roads: List[Dict[str, Any]] = []
    places: List[Dict[str, Any]] = []

    for way in ways:
        tags = way.get("tags", {})
        if "highway" in tags:
            geom = get_way_geometry(way)
            if len(geom) >= 2:
                highway_type = tags["highway"]
                roads.append({
                    "id": way["id"],
                    "name": tags.get("name", tags.get("ref", "Unnamed Road")),
                    "highway": highway_type,
                    "is_main_road": highway_type in MAIN_ROAD_TYPES,
                    "geometry": [list(pt) for pt in geom],
                })

    for way in ways:
        tags = way.get("tags", {})
        if _is_place_of_interest(tags):
            geom = get_way_geometry(way)
            if geom:
                avg_lat = sum(p[0] for p in geom) / len(geom)
                avg_lon = sum(p[1] for p in geom) / len(geom)
                places.append(_create_place_entry(way, tags, avg_lat, avg_lon))
            elif "center" in way:
                places.append(_create_place_entry(way, tags, way["center"]["lat"], way["center"]["lon"]))

    for el in elements:
        if el.get("type") == "node" and "lat" in el and "lon" in el:
            tags = el.get("tags", {})
            if _is_place_of_interest(tags):
                places.append(_create_place_entry(el, tags, el["lat"], el["lon"]))

    return {"roads": roads, "places": places}


# =========================================================
# ENRICHMENT
# =========================================================

def nearest_road_for_project(
    lat: float,
    lon: float,
    roads: List[Dict[str, Any]],
    ref_lat: float,
    ref_lon: float,
    allowed_types: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_dist = float("inf")
    best_proj: Optional[Tuple[float, float]] = None

    for road in roads:
        if allowed_types is not None and road["highway"] not in allowed_types:
            continue
        dist, proj = point_to_polyline_distance_m(lat, lon, road["geometry"], ref_lat, ref_lon)
        if dist is not None and dist < best_dist:
            best_dist = dist
            best = road
            best_proj = proj

    if best is None:
        return None

    return {
        "nearest_road_name": best["name"],
        "nearest_road_type": best["highway"],
        "distance_from_road_m": round(best_dist, 2),
        "nearest_road_point_lat": best_proj[0] if best_proj else None,
        "nearest_road_point_lon": best_proj[1] if best_proj else None,
    }


def nearest_place_distance(
    lat: float,
    lon: float,
    places: List[Dict[str, Any]],
    filter_fn: Optional[Any] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    best: Optional[Dict[str, Any]] = None
    best_dist = float("inf")
    for p in places:
        if filter_fn and not filter_fn(p):
            continue
        d = haversine_m(lat, lon, p["lat"], p["lon"])
        if d < best_dist:
            best_dist = d
            best = p
    if best is None:
        return None, None
    return best, round(best_dist, 2)


def enrich_projects(
    df: pd.DataFrame,
    osm_data: Dict[str, List[Dict[str, Any]]],
    planner: Dict[str, Any],
    progress_log: List[str],
) -> pd.DataFrame:
    roads = osm_data["roads"]
    places = osm_data["places"]
    ref_lat = df["lat"].mean()
    ref_lon = df["long"].mean()

    need_road = planner.get("needs", {}).get("road_distance", True)
    need_amenities = planner.get("needs", {}).get("amenities", False)
    need_infra = planner.get("needs", {}).get("infrastructure", False)

    out = df.copy()

    if need_road:
        progress_log.append("Calculating nearest road (any) and nearest main road distances.")

        def _any_road(row: Any) -> Optional[Dict[str, Any]]:
            return nearest_road_for_project(row["lat"], row["long"], roads, ref_lat, ref_lon)

        def _main_road(row: Any) -> Optional[Dict[str, Any]]:
            return nearest_road_for_project(row["lat"], row["long"], roads, ref_lat, ref_lon, allowed_types=MAIN_ROAD_TYPES)

        any_road_results = out.apply(_any_road, axis=1)
        out["nearest_road_name"] = any_road_results.apply(lambda x: x["nearest_road_name"] if x else None)
        out["nearest_road_type"] = any_road_results.apply(lambda x: x["nearest_road_type"] if x else None)
        out["distance_from_road_m"] = any_road_results.apply(lambda x: x["distance_from_road_m"] if x else None)
        out["nearest_road_point_lat"] = any_road_results.apply(lambda x: x["nearest_road_point_lat"] if x else None)
        out["nearest_road_point_lon"] = any_road_results.apply(lambda x: x["nearest_road_point_lon"] if x else None)

        main_road_results = out.apply(_main_road, axis=1)
        out["nearest_main_road_name"] = main_road_results.apply(lambda x: x["nearest_road_name"] if x else None)
        out["nearest_main_road_type"] = main_road_results.apply(lambda x: x["nearest_road_type"] if x else None)
        out["distance_from_main_road_m"] = main_road_results.apply(lambda x: x["distance_from_road_m"] if x else None)
        out["nearest_main_road_point_lat"] = main_road_results.apply(lambda x: x["nearest_road_point_lat"] if x else None)
        out["nearest_main_road_point_lon"] = main_road_results.apply(lambda x: x["nearest_road_point_lon"] if x else None)

        out["zone"] = out["distance_from_main_road_m"].apply(lambda x: classify_zone(x) if pd.notnull(x) else None)

    if need_amenities:
        progress_log.append("Calculating nearest amenity distances.")
        amenity_types = {"school", "hospital", "college", "university", "bank", "pharmacy", "marketplace", "clinic"}
        amenity_filter = lambda p: p.get("amenity") in amenity_types
        out["nearest_amenity_name"] = out.apply(
            lambda r: (nearest_place_distance(r["lat"], r["long"], places, amenity_filter)[0] or {}).get("name"), axis=1
        )
        out["distance_to_amenity_m"] = out.apply(
            lambda r: nearest_place_distance(r["lat"], r["long"], places, amenity_filter)[1], axis=1
        )

    if need_infra:
        progress_log.append("Calculating nearest infrastructure distances.")
        infra_filter = lambda p: (
            p.get("railway") == "station"
            or p.get("highway") == "bus_stop"
            or p.get("public_transport") == "station"
            or p.get("amenity") == "bus_station"
        )
        out["nearest_infra_name"] = out.apply(
            lambda r: (nearest_place_distance(r["lat"], r["long"], places, infra_filter)[0] or {}).get("name"), axis=1
        )
        out["distance_to_infra_m"] = out.apply(
            lambda r: nearest_place_distance(r["lat"], r["long"], places, infra_filter)[1], axis=1
        )

    return out


def estimate_subject_rate(
    subject_lat: float,
    subject_lon: float,
    df_with_rates: pd.DataFrame,
    progress_log: List[str],
) -> Tuple[Optional[float], List[str]]:
    known = df_with_rates.dropna(subset=["rate"]).copy()
    if known.empty or "distance_from_main_road_m" not in known.columns:
        return None, []

    known["dist_to_subject_m"] = known.apply(
        lambda row: haversine_m(subject_lat, subject_lon, row["lat"], row["long"]), axis=1
    )
    nearest_geo = known.nsmallest(5, "dist_to_subject_m")
    if nearest_geo.empty:
        return None, []

    nearest = nearest_geo.nsmallest(3, "dist_to_subject_m")
    eps = 1.0
    weights = 1 / (nearest["dist_to_subject_m"] + eps)
    estimated_rate = float((nearest["rate"] * weights).sum() / weights.sum())
    comparable_names = nearest["project_name"].tolist()
    progress_log.append(f"Estimated subject rate: {estimated_rate:.2f} using comparables: {comparable_names}")
    return estimated_rate, comparable_names


def add_comparables(df: pd.DataFrame, progress_log: List[str]) -> pd.DataFrame:
    progress_log.append("Finding comparable projects based on proximity and rate similarity.")
    out = df.copy()
    comp_names: List[Optional[str]] = []
    comp_scores: List[Optional[float]] = []

    for idx, row in out.iterrows():
        best_name: Optional[str] = None
        best_score = float("inf")
        for jdx, other in out.iterrows():
            if idx == jdx:
                continue
            geo_d = haversine_m(row["lat"], row["long"], other["lat"], other["long"])
            rate_d = abs(float(row["rate"]) - float(other["rate"])) if pd.notnull(row.get("rate")) and pd.notnull(other.get("rate")) else 0
            road_d = 0.0
            if pd.notnull(row.get("distance_from_main_road_m")) and pd.notnull(other.get("distance_from_main_road_m")):
                road_d = abs(float(row["distance_from_main_road_m"]) - float(other["distance_from_main_road_m"]))
            score = geo_d * 0.5 + rate_d * 0.03 + road_d * 0.2
            if score < best_score:
                best_score = score
                best_name = other["project_name"]
        comp_names.append(best_name)
        comp_scores.append(round(best_score, 2) if best_name else None)

    out["comparable_project"] = comp_names
    out["comparable_score"] = comp_scores
    return out


# =========================================================
# LLM PROMPTS
# =========================================================

def build_plan_prompt(
    query: str,
    mapping: Dict[str, str],
    sample_data: List[Dict[str, Any]],
    subject_info: Optional[Dict[str, Any]] = None,
) -> str:
    subject_text = ""
    if subject_info:
        subject_text = f"Subject project: {subject_info.get('name', 'User Subject')} at ({subject_info['lat']}, {subject_info['lon']})"
    return f"""
User request:
{query}
{subject_text}

Excel Columns Mapping Dictionary (Column Name : Meaning):
{json.dumps(mapping, indent=2)}

Sample transaction data (from Excel):
{json.dumps(sample_data, indent=2)}

Create an implementation plan. 
IMPORTANT: The transaction dataset is very granular (multiple records per project).
You must define `data_filter_logic` to group data to the project-level (`project_name`, `project_latitude`, `project_longitude`).

Return JSON in this exact structure:
{{
  "scope_of_work": ["..."],
  "data_filter_logic": {{
     "filters": [
        {{"column": "unit_configuration", "operator": "contains", "value": "2 BHK"}},
        {{"column": "year", "operator": "==", "value": 2024}}
     ],
     "groupby": ["project_name", "project_latitude", "project_longitude"],
     "rate_numerator": "agreement_price",
     "rate_denominator": "net_carpet_area_sq_m",
     "aggregation_method": "mean"
  }},
  "map_data_to_extract": ["roads", "amenities", "infrastructure"],
  "needs": {{
    "road_distance": true,
    "amenities": false,
    "infrastructure": false
  }},
  "implementation_steps": ["step 1 ..."]
}}

Rules:
- Output valid JSON only.
- Decide filters based ONLY on user request (e.g., if they ask for '2 BHK' or '2023', add a filter).
- "operator" can be "==", "!=", ">", "<", ">=", "<=", "contains".
"""


def build_insight_prompt(
    query: str,
    planner: Dict[str, Any],
    enriched_df: pd.DataFrame,
    subject_info: Optional[Dict[str, Any]] = None,
) -> str:
    sample = enriched_df.head(50).to_dict(orient="records")
    subject_text = ""
    if subject_info:
        subject_text = f"""
Subject project (user-provided): {subject_info.get('name', 'Subject')} at ({subject_info['lat']}, {subject_info['lon']})
Estimated Rate: {subject_info.get('estimated_rate', 'N/A')}
Comparables used: {subject_info.get('comparables', [])}
Distance to nearest main road: {subject_info.get('distance_to_main_road_m', 'N/A')} m
Nearest main road: {subject_info.get('nearest_main_road_name', 'N/A')} ({subject_info.get('nearest_main_road_type', '')})
Zone: {subject_info.get('zone', 'N/A')}
"""
    return f"""
User request:
{query}
{subject_text}

Scope and implementation plan:
{json.dumps(planner, indent=2)}

Computed output sample:
{json.dumps(sample, indent=2)}

Write final insights using these exact headings:
Introduction
Analysis
Conclusion

Focus on:
- what was calculated (distance to nearest **main road**)
- what pattern is visible
- whether main road distance affects rates
- whether amenities/infrastructure/comparables add useful context
- what the user can conclude from the current run
- if a subject project was provided, provide specific insights for that location, including its estimated rate and how it compares to the Excel projects.
"""


# =========================================================
# MAIN ENTRY POINT
# =========================================================

def run_spatial_analysis(
    user_query: str,
    subject_name: str,
    subject_lat: Optional[float],
    subject_lon: Optional[float],
    use_subject: bool,
    excel_path: str,
    mapping_path: str,
) -> Dict[str, Any]:
    """
    Run the full spatial analysis workflow and return structured data
    that the frontend can render (maps, tables, logs, insights).
    """
    token_log: List[Dict[str, Any]] = []
    progress_log: List[str] = []

    # 1. Load Excel transaction data and mapping
    raw_df, mapping = load_transaction_data(excel_path, mapping_path)
    progress_log.append(f"Loaded {len(raw_df)} transaction rows from database.")

    # 2. Handle subject project
    subject_info: Optional[Dict[str, Any]] = None
    if use_subject and subject_lat is not None and subject_lon is not None:
        subject_info = {
            "name": subject_name,
            "lat": subject_lat,
            "lon": subject_lon,
        }
        progress_log.append(f"Subject project '{subject_name}' at ({subject_lat:.5f}, {subject_lon:.5f}) included.")

    # 3. LLM — Planning + Data Filtering Logic
    sample_data = json.loads(raw_df.head(5).to_json(orient="records", date_format="iso"))
    planner_prompt = build_plan_prompt(user_query, mapping, sample_data, subject_info)
    planner = llm_call_json("1. Requirement & Scope Planning", planner_prompt, token_log)
    progress_log.append("LLM deciphered requirement and created implementation plan & filter logic.")

    # 3b. Apply Data Filtering Logic
    filter_logic = planner.get("data_filter_logic", {})
    df = apply_data_filter(raw_df, filter_logic, progress_log)
    
    # Cap processing points to prevent N^2 spatial explosion
    if len(df) > 80:
        df = df.head(80)
        progress_log.append("Capped dataset to 80 project entries to ensure stable and fast spatial processing.")

    excel_preview = json.loads(df.head(12).to_json(orient="records", date_format="iso"))
    
    if df.empty:
        raise ValueError("The filtering logic resulted in 0 properties. Please try a less strict query.")

    if use_subject and subject_lat is not None and subject_lon is not None:
        subject_row = pd.DataFrame([{
            "project_name": subject_name,
            "lat": subject_lat,
            "long": subject_lon,
            "rate": None,
        }])
        df = pd.concat([df, subject_row], ignore_index=True)

    # 4. Fetch OSM data
    needs = planner.get("needs", {})
    need_amenities = needs.get("amenities", False)
    need_infra = needs.get("infrastructure", False)

    s_lat_for_bbox = subject_lat if (use_subject and subject_lat is not None) else None
    s_lon_for_bbox = subject_lon if (use_subject and subject_lon is not None) else None

    min_lat, min_lon, max_lat, max_lon = bbox_from_projects(df, s_lat_for_bbox, s_lon_for_bbox)
    progress_log.append("Prepared project bounding box for efficient OSM extraction.")
    osm_payload = fetch_osm_bulk(min_lat, min_lon, max_lat, max_lon, need_amenities, need_infra)
    osm_data = parse_osm_payload(osm_payload)
    progress_log.append(f"Fetched OSM context: {len(osm_data['roads'])} roads, {len(osm_data['places'])} places.")

    # 5. Enrich projects
    enriched_df = enrich_projects(df, osm_data, planner, progress_log)

    # 6. Subject rate estimation
    if subject_info and not enriched_df.empty:
        subject_row_mask = (enriched_df["project_name"] == subject_name) & (enriched_df["rate"].isna())
        if subject_row_mask.any():
            known_df = enriched_df[enriched_df["rate"].notna()]
            estimated_rate, comparables = estimate_subject_rate(subject_lat, subject_lon, known_df, progress_log)
            if estimated_rate is not None:
                subject_info["estimated_rate"] = round(estimated_rate, 2)
                subject_info["comparables"] = comparables
                enriched_df.loc[subject_row_mask, "rate"] = estimated_rate

            sub_row = enriched_df[subject_row_mask].iloc[0]
            subject_info["distance_to_main_road_m"] = sub_row.get("distance_from_main_road_m")
            subject_info["zone"] = sub_row.get("zone")
            subject_info["nearest_main_road_name"] = sub_row.get("nearest_main_road_name")
            subject_info["nearest_main_road_type"] = sub_row.get("nearest_main_road_type")
            subject_info["nearest_main_road_point_lat"] = sub_row.get("nearest_main_road_point_lat")
            subject_info["nearest_main_road_point_lon"] = sub_row.get("nearest_main_road_point_lon")

    # 7. Comparables / valuation
    if needs.get("comparables", False):
        enriched_df = add_comparables(enriched_df, progress_log)
    if needs.get("valuation", False):
        progress_log.append("Valuation-specific logic was requested; spatial drivers and comparable context prepared.")

    progress_log.append("All calculations completed in temporary memory. Excel source was not modified.")

    # 8. Execution summary — built directly from progress_log (no LLM call needed;
    #    the progress_log already captures every workflow step in plain language).
    outputs_generated = [
        f"Enriched dataset ({len(enriched_df)} projects)",
        f"Road proximity analysis ({len(osm_data['roads'])} roads)",
        "Zone classification (Premium / High Value / Balanced / Discount)",
        "LLM spatial insights",
    ]
    if subject_info:
        outputs_generated.append(f"Subject project rate estimate for '{subject_name}'")
    execution_summary = {
        "execution_summary": progress_log[:],
        "outputs_generated": outputs_generated,
    }

    # 9. LLM — Insights  (only remaining LLM call after planning)
    insight_prompt = build_insight_prompt(user_query, planner, enriched_df, subject_info)
    insights = llm_call_text("2. Final Insight Generation", insight_prompt, token_log)
    progress_log.append("LLM generated final business insights.")

    # 10. Serialise enriched_df safely
    enriched_records = json.loads(enriched_df.to_json(orient="records", date_format="iso"))

    return {
        "projects": enriched_records,
        "roads": osm_data["roads"][:500],          # cap for response size
        "places": osm_data["places"][:300],
        "planner": planner,
        "execution_summary": execution_summary,
        "insights": insights,
        "subject_info": subject_info,
        "token_log": token_log,
        "progress_log": progress_log,
        "excel_preview": excel_preview,
        "stats": {
            "project_count": len(df),
            "road_count": len(osm_data["roads"]),
            "place_count": len(osm_data["places"]),
            "total_cost_usd": round(sum(t["cost_usd"] for t in token_log), 6),
        },
    }
