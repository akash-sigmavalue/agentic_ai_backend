"""
Built-Up Density & Congestion Analysis Tool
============================================
Usage:
    python builtup_density_tool.py --lat 18.6298 --lng 73.7997 --radius 500

Or import and call directly:
    from builtup_density_tool import analyze_congestion
    result = analyze_congestion(lat=18.6298, lng=73.7997, radius=500)

Dependencies:
    pip install shapely pyproj requests
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import requests
from pyproj import Transformer
from shapely.geometry import LineString, Polygon, Point
from shapely.validation import make_valid


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
TIMEOUT = 90
RETRY = 3
USER_AGENT = "BuiltUpDensityTool/1.0"

DEFAULT_WIDTHS: Dict[str, float] = {
    "motorway": 14.0,
    "motorway_link": 7.0,
    "trunk": 12.0,
    "trunk_link": 6.0,
    "primary": 10.0,
    "primary_link": 5.0,
    "secondary": 8.0,
    "secondary_link": 4.5,
    "tertiary": 7.0,
    "tertiary_link": 4.0,
    "residential": 6.0,
    "living_street": 4.5,
    "service": 4.0,
    "unclassified": 6.0,
    "track": 3.5,
    "footway": 2.0,
    "cycleway": 2.0,
    "path": 1.5,
}

BUILDING_TYPE_MAP = {
    "yes": "building=generic",
    "house": "building=residential",
    "apartments": "building=residential",
    "residential": "building=residential",
    "commercial": "building=commercial",
    "retail": "building=commercial",
    "office": "building=commercial",
    "industrial": "building=industrial",
    "warehouse": "building=industrial",
    "school": "building=civic",
    "hospital": "building=civic",
    "church": "building=civic",
    "civic": "building=civic",
    "public": "building=civic",
    "university": "building=civic",
    "garage": "building=utility",
    "shed": "building=utility",
    "hut": "building=utility",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def projector(lat: float, lng: float) -> Tuple[Transformer, Transformer]:
    """
    Returns (fwd, inv) transformers:
      fwd: WGS84 lon/lat → local azimuthal equidistant metres
      inv: local metres → WGS84 lon/lat
    """
    proj_str = (
        f"+proj=aeqd +lat_0={lat} +lon_0={lng} +datum=WGS84 +units=m +no_defs"
    )
    fwd = Transformer.from_crs("EPSG:4326", proj_str, always_xy=True)
    inv = Transformer.from_crs(proj_str, "EPSG:4326", always_xy=True)
    return fwd, inv


def buffer_circle(lat: float, lng: float, radius: float) -> Polygon:
    """
    Returns a circle polygon in projected (metre) space centred at origin.
    """
    return Point(0.0, 0.0).buffer(radius, resolution=64)


def get_building_type(tags: Dict[str, str]) -> str:
    raw = str(tags.get("building", "yes")).lower()
    return BUILDING_TYPE_MAP.get(raw, f"building={raw}")


def geom_to_latlng_coords(geom, inv: Transformer) -> List[List[float]]:
    """
    Converts a Shapely geometry in projected CRS to [[lat, lng], …] list.
    """
    if geom.is_empty or not geom.is_valid:
        return []

    def _t(x, y):
        lon, lat = inv.transform(x, y)
        return [lat, lon]

    if geom.geom_type == "Polygon":
        return [_t(x, y) for x, y in geom.exterior.coords]
    if geom.geom_type == "MultiPolygon" and geom.geoms:
        return [_t(x, y) for x, y in geom.geoms[0].exterior.coords]
    if geom.geom_type == "LineString":
        return [_t(x, y) for x, y in geom.coords]
    return []


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------

def _build_overpass_query(lat: float, lng: float, radius: float) -> str:
    delta = radius / 111_000 * 2.0
    s, w, n, e = lat - delta, lng - delta, lat + delta, lng + delta
    return f"""
[out:json][timeout:{TIMEOUT}];
(
  way["building"]({s},{w},{n},{e});
  way["highway"]({s},{w},{n},{e});
  way["natural"="water"]({s},{w},{n},{e});
  way["waterway"="riverbank"]({s},{w},{n},{e});
);
out geom;
"""


def overpass_query(query: str) -> Dict:
    headers = {"User-Agent": USER_AGENT}
    for _ in range(RETRY):
        for url in OVERPASS_ENDPOINTS:
            try:
                r = requests.post(
                    url, data={"data": query}, headers=headers, timeout=TIMEOUT
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception:
                time.sleep(1)
    raise RuntimeError("Overpass API failed after all retries.")


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_buildings(
    data: Dict, fwd: Transformer, buf: Polygon
) -> List[Tuple[Polygon, str]]:
    buildings = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if not tags.get("building"):
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 3:
            continue
        coords = [(p["lon"], p["lat"]) for p in geom]
        xs, ys = zip(*[fwd.transform(lon, lat) for lon, lat in coords])
        try:
            poly = Polygon(list(zip(xs, ys)))
            if not poly.is_valid:
                poly = make_valid(poly)
            if not poly.is_valid:
                continue
            inter = poly.intersection(buf)
            if inter.is_empty:
                continue
            if not inter.is_valid:
                inter = make_valid(inter)
            if not inter.is_valid:
                continue
            buildings.append((inter, get_building_type(tags)))
        except Exception:
            continue
    return buildings


def collect_roads(data: Dict, fwd: Transformer) -> List[Dict]:
    roads = []
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags", {})
        if "highway" not in tags:
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        coords = [(p["lon"], p["lat"]) for p in geom]
        xs, ys = zip(*[fwd.transform(lon, lat) for lon, lat in coords])
        line = LineString(list(zip(xs, ys)))
        if line.length < 1:
            continue

        width, width_source = None, None
        if "width" in tags:
            try:
                width = max(3.0, float(str(tags["width"]).split()[0]))
                width_source = "osm"
            except Exception:
                pass
        if width is None and "lanes" in tags:
            try:
                lanes = float(str(tags["lanes"]).split()[0])
                width = max(7.0, lanes * 3.5)
                width_source = "lanes"
            except Exception:
                pass
        if width is None:
            width = DEFAULT_WIDTHS.get(tags.get("highway", ""), 7.0)
            width_source = "fallback"

        roads.append(
            {
                "geom": line,
                "width": width,
                "width_source": width_source,
                "highway": tags.get("highway", ""),
                "name": tags.get("name", ""),
            }
        )
    return roads


def collect_water(
    data: Dict, fwd: Transformer, buf: Polygon
) -> Tuple[float, List[Polygon]]:
    """Returns (total_water_area_m2, list_of_water_polygons)."""
    total = 0.0
    polys = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if tags.get("natural") != "water" and tags.get("waterway") != "riverbank":
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 3:
            continue
        coords = [(p["lon"], p["lat"]) for p in geom]
        xs, ys = zip(*[fwd.transform(lon, lat) for lon, lat in coords])
        try:
            poly = Polygon(list(zip(xs, ys)))
            if not poly.is_valid:
                poly = make_valid(poly)
            if not poly.is_valid:
                continue
            inter = poly.intersection(buf)
            if not inter.is_empty:
                total += inter.area
                polys.append(inter)
        except Exception:
            continue
    return total, polys


def clip_and_compute_roads(
    roads_raw: List[Dict], buf: Polygon
) -> Tuple[float, List[Dict]]:
    road_area_total = 0.0
    road_details = []
    for rd in roads_raw:
        line = rd["geom"]
        width = rd["width"]
        source = rd.get("width_source")
        clipped = line.intersection(buf)
        if clipped.is_empty or clipped.length < 1:
            continue
        segments = list(clipped.geoms) if hasattr(clipped, "geoms") else [clipped]
        for seg in segments:
            if width and seg.length > 0:
                road_area_total += seg.length * width
            final_source = source if source in ("osm", "lanes") else (
                "osm" if (width is not None and width >= 3.0) else "fallback"
            )
            road_details.append(
                {
                    "geom": seg,
                    "width": width,
                    "width_source": final_source,
                    "highway": rd.get("highway", ""),
                    "name": rd.get("name", ""),
                }
            )
    return road_area_total, road_details


def compute_building_type_areas(
    buildings_with_type: List[Tuple[Polygon, str]]
) -> Dict[str, float]:
    type_areas: Dict[str, float] = {}
    for geom, btype in buildings_with_type:
        type_areas[btype] = type_areas.get(btype, 0.0) + geom.area
    return type_areas


# ---------------------------------------------------------------------------
# Main analysis entry point
# ---------------------------------------------------------------------------

def analyze_congestion(lat: float, lng: float, radius: float) -> Dict[str, Any]:
    """
    Full built-up density and congestion analysis.

    Parameters
    ----------
    lat    : float  – latitude of centre point
    lng    : float  – longitude of centre point
    radius : float  – analysis radius in metres

    Returns
    -------
    dict with keys:
        lat, lng, radius
        congestion   – score (0-10), level (LOW/MEDIUM/HIGH), ratios
        metrics      – area breakdowns, coverage ratios, building counts
        summary      – human-readable text summary
        mapData      – buildings / roads / water coordinate lists (for Leaflet etc.)
        timestamp
    """
    print(f"[analyze_congestion] lat={lat}, lng={lng}, radius={radius}m")

    # 1. Projection
    fwd, inv = projector(lat, lng)
    buf = buffer_circle(lat, lng, radius)

    # 2. Fetch OSM data
    query = _build_overpass_query(lat, lng, radius)
    data = overpass_query(query)
    n_elements = len(data.get("elements", []))
    print(f"[analyze_congestion] {n_elements} OSM elements received")

    # 3. Collect features
    buildings_with_type = collect_buildings(data, fwd, buf)
    roads_raw = collect_roads(data, fwd)
    water_area_m2, water_polygons = collect_water(data, fwd, buf)

    # 4. Clip roads
    road_area_total, road_details = clip_and_compute_roads(roads_raw, buf)

    # 5. Core metrics
    total_area_m2 = float(3.14159265 * radius ** 2)
    total_building_area = float(sum(p.area for p, _ in buildings_with_type))
    total_road_area = float(road_area_total)
    water_area = float(water_area_m2)

    effective_land_area = max(total_area_m2 - water_area, 1.0)
    used_area = total_building_area + total_road_area
    used_area_ratio = used_area / effective_land_area
    true_open_space = max(effective_land_area - total_building_area - total_road_area, 0.0)

    # 6. Building type breakdown
    type_areas = compute_building_type_areas(buildings_with_type)

    # 7. Congestion score  (z-score normalised around 30 % used ratio ± 25 %)
    z = max(-2.0, min(2.0, (used_area_ratio - 0.30) / 0.25))
    congestion_score = round(max(0.0, min(10.0, z * 5.0 + 5.0)), 1)
    if congestion_score > 7:
        level = "HIGH"
    elif congestion_score > 4:
        level = "MEDIUM"
    else:
        level = "LOW"

    # 8. Density classification
    bcr = total_building_area / total_area_m2 if total_area_m2 > 0 else 0.0
    if bcr > 0.50:
        density_class = "Very High Density"
    elif bcr > 0.35:
        density_class = "High Density"
    elif bcr > 0.20:
        density_class = "Medium Density"
    elif bcr > 0.08:
        density_class = "Low Density"
    else:
        density_class = "Very Low / Rural"

    # 9. Map data
    buildings_json = []
    for geom, btype in buildings_with_type:
        coords = geom_to_latlng_coords(geom, inv)
        if not coords or len(coords) < 3:
            continue
        buildings_json.append(
            {
                "tag": btype,
                "name": btype.replace("building=", "").title().replace("Yes", "Generic Building"),
                "area_m2": round(float(geom.area), 2),
                "coordinates": coords,
            }
        )

    roads_json = []
    for rd in road_details:
        coords = geom_to_latlng_coords(rd["geom"], inv)
        if not coords or len(coords) < 2:
            continue
        roads_json.append(
            {
                "highway": rd.get("highway", ""),
                "name": rd.get("name", ""),
                "length_m": round(float(rd["geom"].length), 2),
                "width_m": round(float(rd.get("width") or 0.0), 2),
                "width_source": rd.get("width_source"),
                "coordinates": coords,
            }
        )

    water_json = []
    for poly in water_polygons:
        if poly.is_empty or not poly.is_valid or poly.area < 200:
            continue
        geoms = list(poly.geoms) if hasattr(poly, "geoms") else [poly]
        for g in geoms:
            coords = geom_to_latlng_coords(g, inv)
            if not coords or len(coords) < 3:
                continue
            water_json.append({"area_m2": round(float(g.area), 2), "coordinates": coords})

    # 10. Human-readable summary
    summary_lines = [
        f"Analysis area   : {radius}m radius around ({lat:.5f}, {lng:.5f})",
        f"Total area      : {total_area_m2:,.0f} m²  ({total_area_m2/10_000:.2f} ha)",
        f"",
        f"── Built-Up Density ──────────────────────────────",
        f"  Buildings detected   : {len(buildings_with_type)}",
        f"  Building area        : {total_building_area:,.0f} m²  (BCR {bcr*100:.1f}%)",
        f"  Density class        : {density_class}",
        f"",
        f"── Road Coverage ────────────────────────────────",
        f"  Road segments        : {len(road_details)}",
        f"  Road area            : {total_road_area:,.0f} m²  ({total_road_area/total_area_m2*100:.1f}%)",
        f"",
        f"── Open Space & Water ───────────────────────────",
        f"  Water area           : {water_area:,.0f} m²  ({water_area/total_area_m2*100:.1f}%)",
        f"  True open space      : {true_open_space:,.0f} m²  ({true_open_space/total_area_m2*100:.1f}%)",
        f"",
        f"── Congestion ───────────────────────────────────",
        f"  Used-area ratio      : {used_area_ratio*100:.1f}%",
        f"  Congestion score     : {congestion_score} / 10  [{level}]",
        f"",
        f"── Building Type Breakdown ──────────────────────",
    ]
    for btype, area in sorted(type_areas.items(), key=lambda x: -x[1]):
        pct = area / total_building_area * 100 if total_building_area > 0 else 0
        summary_lines.append(f"  {btype:<30s}: {area:>10,.0f} m²  ({pct:.1f}%)")

    summary = "\n".join(summary_lines)

    # 11. Final response dict
    return {
        "lat": lat,
        "lng": lng,
        "radius": radius,
        "congestion": {
            "score": congestion_score,
            "level": level,
            "used_area_ratio": round(used_area_ratio, 4),
            "true_open_space_m2": round(true_open_space, 2),
            "effective_land_area_m2": round(effective_land_area, 2),
            "water_share_ratio": round(water_area / total_area_m2, 4) if total_area_m2 else 0.0,
        },
        "metrics": {
            "analysis_area_m2": round(total_area_m2, 2),
            "total_building_area_m2": round(total_building_area, 2),
            "building_coverage_ratio": round(bcr, 4),
            "density_class": density_class,
            "total_road_area_m2": round(total_road_area, 2),
            "road_area_coverage": round(total_road_area / total_area_m2, 4) if total_area_m2 else 0.0,
            "water_area_m2": round(water_area, 2),
            "water_coverage_ratio": round(water_area / total_area_m2, 4) if total_area_m2 else 0.0,
            "true_open_space_m2": round(true_open_space, 2),
            "true_open_space_ratio": round(true_open_space / total_area_m2, 4) if total_area_m2 else 0.0,
            "detected_buildings": len(buildings_with_type),
            "detected_road_segments": len(road_details),
            "building_types_area_m2": {k: round(v, 2) for k, v in type_areas.items()},
        },
        "summary": summary,
        "mapData": {
            "buildings": buildings_json,
            "roads": roads_json,
            "water": water_json,
        },
        "timestamp": datetime.now().strftime("%B %d, %Y • %I:%M %p"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Built-Up Density & Congestion Analyser"
    )
    parser.add_argument("--lat", type=float, required=True, help="Latitude")
    parser.add_argument("--lng", type=float, required=True, help="Longitude")
    parser.add_argument(
        "--radius", type=float, default=500, help="Radius in metres (default 500)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output full JSON (default: summary text)"
    )
    args = parser.parse_args()

    result = analyze_congestion(args.lat, args.lng, args.radius)

    if args.json:
        # Strip mapData coordinates to keep output readable unless needed
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result["summary"])
        print(f"\nTimestamp: {result['timestamp']}")


if __name__ == "__main__":
    main()
