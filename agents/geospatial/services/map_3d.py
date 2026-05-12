from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from geopy.geocoders import ArcGIS, Nominatim
from shapely.geometry import Point, shape

from api.schemas.geospatial.maps import MapLocation, MapSummary, ThreeDMapRequest, ThreeDMapResponse
from core.config import settings

# from core.config import settings
# from app.schemas.maps import MapLocation, MapSummary, ThreeDMapRequest, ThreeDMapResponse

FLOOR_HEIGHT_M = 3.0
SNAP_DISTANCE_M = 100.0
CACHE_DB = Path(__file__).resolve().parents[3] / "database" / "places_cache.db"
DEFAULT_EXCEL_PATH = Path(__file__).resolve().parents[3] / "database" / "transactions_db1.xlsx"

# Nominatim enforces very strict request limits. When we run corrections concurrently,
# requests can still collide and cause HTTP 429. This lock+timer serializes calls
# so we don't exceed ~1 request/sec.
NOMINATIM_MIN_INTERVAL_S = 1.05
_nominatim_lock = threading.Lock()
_nominatim_last_call_ts = 0.0


def init_cache_db() -> None:
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS places_cache (
            building_name TEXT,
            city TEXT,
            original_lat REAL,
            original_lng REAL,
            corrected_lat REAL,
            corrected_lng REAL,
            formatted_address TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (building_name, city)
        )
        """
    )
    conn.commit()
    conn.close()


def get_cached_coordinates(building_name: str, city: str) -> tuple[float, float, str] | None:
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT corrected_lat, corrected_lng, formatted_address FROM places_cache WHERE building_name=? AND city=?",
        (building_name, city),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return (row[0], row[1], row[2])
    return None


def save_cached_coordinates(
    building_name: str,
    city: str,
    original_lat: float,
    original_lng: float,
    corrected_lat: float,
    corrected_lng: float,
    formatted_address: str,
) -> None:
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO places_cache (
            building_name, city, original_lat, original_lng, corrected_lat, corrected_lng, formatted_address, timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            building_name,
            city,
            original_lat,
            original_lng,
            corrected_lat,
            corrected_lng,
            formatted_address,
            datetime.now(),
        ),
    )
    conn.commit()
    conn.close()


def geocode_place(place_name: str, city_hint: str | None = None) -> tuple[float, float, str]:
    query = place_name.strip()
    if city_hint and city_hint.strip():
        query = f"{query}, {city_hint.strip()}"

    if settings.GOOGLE_MAPS_API_KEY:
        response = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": settings.GOOGLE_MAPS_API_KEY},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") == "OK" and payload.get("results"):
            result = payload["results"][0]
            location = result["geometry"]["location"]
            return location["lat"], location["lng"], result["formatted_address"]

    # Prefer ArcGIS first; fall back to Nominatim (free).
    try:
        geolocator = ArcGIS()
        location = geolocator.geocode(query, timeout=8)
        if location:
            return location.latitude, location.longitude, location.address
    except Exception:
        pass

    try:
        location = _safe_nominatim_geocode(query, timeout=15)
        if not location:
            raise ValueError()
        return location.latitude, location.longitude, location.address
    except Exception:
        raise ValueError(f"Could not geocode location: {query}")


def _safe_nominatim_geocode(place_name: str, timeout: int):
    """
    Serialize Nominatim calls across threads to avoid HTTP 429.
    """
    global _nominatim_last_call_ts
    with _nominatim_lock:
        now = time.time()
        elapsed = now - _nominatim_last_call_ts
        if elapsed < NOMINATIM_MIN_INTERVAL_S:
            time.sleep(NOMINATIM_MIN_INTERVAL_S - elapsed)
        _nominatim_last_call_ts = time.time()

        geolocator = Nominatim(user_agent="ai_agent_dynamic_3d_maps")
        return geolocator.geocode(place_name, timeout=timeout)


def radius_to_bbox(lat: float, lng: float, radius_m: int) -> tuple[float, float, float, float]:
    lat_delta = radius_m / 111320.0
    lng_delta = radius_m / (111320.0 * math.cos(math.radians(lat)) + 1e-12)
    return lng - lng_delta, lat - lat_delta, lng + lng_delta, lat + lat_delta


def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_overture_height(properties: dict[str, Any]) -> float:
    height = properties.get("height")
    num_floors = properties.get("num_floors")
    try:
        if height is not None:
            parsed_height = float(height)
            if parsed_height > 0:
                return max(parsed_height, 4.0)
    except Exception:
        pass
    try:
        if num_floors is not None:
            parsed_floors = float(num_floors)
            if parsed_floors > 0:
                return max(parsed_floors * 3.2, 4.0)
    except Exception:
        pass
    return 15.0


def get_excel_path() -> Path:
    return Path(settings.THREE_D_MAPS_EXCEL_PATH or DEFAULT_EXCEL_PATH)


def _parse_floor_number(value: Any) -> int | None:
    """Convert a floor_number cell to an integer.

    Returns None for values that cannot be mapped to a numeric floor.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text in ("", "nan"):
        return None
    if text == "ground":
        return 0
    if text in ("stilt", "basement"):
        return None  # not a livable floor – skip
    try:
        return int(float(text))
    except (ValueError, TypeError):
        return None


def load_excel_buildings(excel_path: str | os.PathLike[str], warnings_list: list[str]) -> list[dict[str, Any]]:
    if not os.path.exists(excel_path):
        warnings_list.append(f"Excel file not found at {excel_path}. No custom buildings.")
        return []

    try:
        dataframe = pd.read_excel(excel_path)
        required_cols = {"project_name", "project_latitude", "project_longitude", "floor_number", "agreement_price", "net_carpet_area_sq_m"}
        if not required_cols.issubset(dataframe.columns):
            warnings_list.append(f"Excel must contain columns: {required_cols}. Found: {list(dataframe.columns)}")
            return []

        # --- First pass: collect parsed transaction rows grouped by project_name ---
        # For each project we keep:
        # - all lat/lng observations (to derive one representative coordinate)
        # - all (floor, rate) observations from transactions
        grouped: dict[str, dict[str, Any]] = {}
        skipped = 0

        for _, row in dataframe.iterrows():
            try:
                # Skip rows with null project_name
                raw_name = row["project_name"]
                if pd.isna(raw_name) or str(raw_name).strip() == "":
                    skipped += 1
                    continue
                name = str(raw_name).strip()

                # Skip rows with null lat/lng
                if pd.isna(row["project_latitude"]) or pd.isna(row["project_longitude"]):
                    continue
                lat = float(row["project_latitude"])
                lng = float(row["project_longitude"])

                # Parse floor number
                floor_num = _parse_floor_number(row["floor_number"])
                if floor_num is None:
                    continue

                # Compute rate = agreement_price / net_carpet_area_sq_m
                agreement_price = row["agreement_price"]
                carpet_area = row["net_carpet_area_sq_m"]
                if pd.isna(agreement_price) or pd.isna(carpet_area) or float(carpet_area) <= 0:
                    continue
                rate = float(agreement_price) / float(carpet_area)

                project_bucket = grouped.setdefault(
                    name,
                    {
                        "latitudes": [],
                        "longitudes": [],
                        "entries": [],
                    },
                )
                project_bucket["latitudes"].append(lat)
                project_bucket["longitudes"].append(lng)
                project_bucket["entries"].append((floor_num, rate))
            except Exception as exc:
                warnings_list.append(f"Skipping row: {exc}")

        if skipped > 0:
            warnings_list.append(f"Skipped {skipped} row(s) with null/empty project_name.")

        # --- Second pass: build building dicts with floor_rates arrays ---
        buildings_dict: dict[str, dict[str, Any]] = {}

        for name, project_bucket in grouped.items():
            entries = project_bucket["entries"]
            if not entries:
                continue

            # Use average coordinates per project_name because source is a
            # transaction dataset and the same project can appear many times.
            latitudes = project_bucket["latitudes"]
            longitudes = project_bucket["longitudes"]
            lat = sum(latitudes) / len(latitudes)
            lng = sum(longitudes) / len(longitudes)

            # Determine floor range for this building
            all_floors = [f for f, _ in entries]
            min_floor = min(all_floors)
            max_floor = max(all_floors)
            total_floors = max(max_floor - min_floor + 1, 1)

            # Accumulate rates per floor index (0-based: index = floor_num - min_floor)
            floor_rate_accum: dict[int, list[float]] = {}
            for floor_num, rate in entries:
                idx = floor_num - min_floor
                if 0 <= idx < total_floors:
                    floor_rate_accum.setdefault(idx, []).append(rate)

            # Build floor_rates array with averaged rates
            floor_rates: list[float | None] = [None] * total_floors
            for idx, rates_list in floor_rate_accum.items():
                floor_rates[idx] = sum(rates_list) / len(rates_list)

            buildings_dict[name] = {
                "name": name,
                "lat": lat,
                "lng": lng,
                "total_floors": total_floors,
                "floor_rates": floor_rates,
            }

        valid_buildings = []
        for building in buildings_dict.values():
            if all(rate is None for rate in building["floor_rates"]):
                warnings_list.append(f"Building {building['name']} has no rate data at all. Skipping.")
                continue
            valid_buildings.append(building)
        return valid_buildings
    except Exception as exc:
        warnings_list.append(f"Failed to read Excel file: {exc}")
        return []


def geocode_arcgis(building_name: str, city: str, debug_log: list[str]) -> tuple[float, float, str] | None:
    geolocator = ArcGIS()
    try:
        location = geolocator.geocode(f"{building_name}, {city}", timeout=5)
        if not location:
            debug_log.append(f"ArcGIS: {building_name} - no results")
            return None
        debug_log.append(f"ArcGIS: {building_name} -> ({location.latitude:.6f}, {location.longitude:.6f})")
        return location.latitude, location.longitude, location.address
    except Exception as exc:
        debug_log.append(f"ArcGIS error for {building_name}: {exc}")
        return None


def geocode_nominatim(building_name: str, city: str, debug_log: list[str]) -> tuple[float, float, str] | None:
    time.sleep(1)
    geolocator = Nominatim(user_agent="ai_agent_dynamic_3d_maps")
    try:
        location = geolocator.geocode(f"{building_name}, {city}", timeout=10)
        if not location:
            debug_log.append(f"Nominatim: {building_name} - no results")
            return None
        debug_log.append(f"Nominatim: {building_name} -> ({location.latitude:.6f}, {location.longitude:.6f})")
        return location.latitude, location.longitude, location.address
    except Exception as exc:
        debug_log.append(f"Nominatim error for {building_name}: {exc}")
        return None


def geocode_with_fallback(building_name: str, city: str, debug_log: list[str]) -> tuple[float, float, str] | None:
    result = geocode_arcgis(building_name, city, debug_log)
    if result:
        return result
    return geocode_nominatim(building_name, city, debug_log)


async def correct_buildings_async(
    buildings_to_correct: list[dict[str, Any]],
    city: str,
    max_concurrent: int,
    debug_log: list[str],
) -> tuple[list[dict[str, Any]], int]:
    semaphore = asyncio.Semaphore(max_concurrent)
    loop = asyncio.get_event_loop()

    async def process_one(building: dict[str, Any]) -> bool:
        async with semaphore:
            original_lat = building["lat"]
            original_lng = building["lng"]
            result = await loop.run_in_executor(None, partial(geocode_with_fallback, building["name"], city, debug_log))
            if not result:
                return False
            corrected_lat, corrected_lng, address = result
            building["lat"] = corrected_lat
            building["lng"] = corrected_lng
            save_cached_coordinates(
                building["name"],
                city,
                original_lat,
                original_lng,
                corrected_lat,
                corrected_lng,
                address,
            )
            return True

    results = await asyncio.gather(*(process_one(building) for building in buildings_to_correct))
    return buildings_to_correct, sum(results)


def match_custom_buildings_to_features(
    features: list[dict[str, Any]],
    custom_buildings: list[dict[str, Any]],
    snap_distance_m: float = SNAP_DISTANCE_M,
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """
    Match Excel-derived buildings to Overture polygon features (exact cover, then centroid snap).
    Shared by static 3D map and timelapse so assignment behavior stays identical.
    """
    if not custom_buildings:
        return {}, []

    polygons: list[tuple[int, Any, Any]] = []
    for idx, feat in enumerate(features):
        geom = feat.get("geometry")
        if geom and geom.get("type") in ["Polygon", "MultiPolygon"]:
            try:
                poly = shape(geom)
                if poly.is_valid:
                    polygons.append((idx, poly, poly.centroid))
            except Exception:
                continue

    matched_indices: dict[int, dict[str, Any]] = {}
    source_tracker: list[str] = []

    for building in custom_buildings:
        point = Point(building["lng"], building["lat"])
        for idx, poly, _ in polygons:
            if poly.covers(point):
                matched_indices[idx] = building
                source_tracker.append("exact_match")
                break

    unmatched_buildings = [b for b in custom_buildings if not any(b is matched for matched in matched_indices.values())]
    if unmatched_buildings:
        centroids = [(idx, (centroid.x, centroid.y)) for idx, _, centroid in polygons]
        for building in unmatched_buildings:
            best_idx = None
            best_distance = float("inf")
            for idx, (cx, cy) in centroids:
                distance = haversine_distance(building["lat"], building["lng"], cy, cx)
                if distance < best_distance and distance <= snap_distance_m:
                    best_idx = idx
                    best_distance = distance
            if best_idx is not None:
                matched_indices[best_idx] = building
                source_tracker.append("snapped")
            else:
                source_tracker.append("unmatched")

    return matched_indices, source_tracker


def annotate_custom_buildings_with_snapping(
    features: list[dict[str, Any]],
    custom_buildings: list[dict[str, Any]],
    snap_distance_m: float = SNAP_DISTANCE_M,
) -> tuple[int, dict[str, int], list[dict[str, Any]]]:
    if not custom_buildings:
        return 0, {"exact_match": 0, "snapped": 0, "unmatched": 0}, features

    matched_indices, source_tracker = match_custom_buildings_to_features(
        features, custom_buildings, snap_distance_m=snap_distance_m
    )

    for idx, building in matched_indices.items():
        existing_rates = [rate for rate in building["floor_rates"] if rate is not None]
        min_rate = min(existing_rates) if existing_rates else 0
        max_rate = max(existing_rates) if existing_rates else 1
        features[idx]["properties"]["is_custom"] = True
        features[idx]["properties"]["building_name"] = building["name"]
        features[idx]["properties"]["num_floors"] = building["total_floors"]
        features[idx]["properties"]["height_render"] = building["total_floors"] * FLOOR_HEIGHT_M
        features[idx]["properties"]["floor_rates"] = building["floor_rates"]
        features[idx]["properties"]["min_rate"] = min_rate
        features[idx]["properties"]["max_rate"] = max_rate

    source_counts = {
        "exact_match": source_tracker.count("exact_match"),
        "snapped": source_tracker.count("snapped"),
        "unmatched": source_tracker.count("unmatched"),
    }
    return source_counts["exact_match"] + source_counts["snapped"], source_counts, features


def fetch_overture_buildings(lat: float, lng: float, radius_m: int = 450) -> dict[str, Any]:
    west, south, east, north = radius_to_bbox(lat, lng, radius_m)
    overture_exe = shutil.which("overturemaps") or shutil.which("overturemaps.exe")
    if not overture_exe:
        raise RuntimeError("Overture CLI not found. Install with: pip install overturemaps")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "overture_buildings.geojson")
        result = subprocess.run(
            [
                overture_exe,
                "download",
                f"--bbox={west},{south},{east},{north}",
                "-f",
                "geojson",
                "--type=building",
                "-o",
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            shell=False,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode != 0:
            raise RuntimeError(f"Overture download failed:\n{result.stderr}")
        with open(output_path, "r", encoding="utf-8") as file:
            geojson = json.load(file)

    features = []
    for feature in geojson.get("features", []):
        geometry = feature.get("geometry")
        if not geometry or geometry.get("type") not in ["Polygon", "MultiPolygon"]:
            continue
        props = feature.get("properties", {}) or {}
        props["height_render"] = parse_overture_height(props)
        props["is_custom"] = False
        props["building_name"] = None
        props["num_floors"] = None
        props["floor_rates"] = None
        props["min_rate"] = None
        props["max_rate"] = None
        feature["properties"] = props
        features.append(feature)
    return {"type": "FeatureCollection", "features": features}


def _run_correction(buildings_to_correct: list[dict[str, Any]], city: str, debug_log: list[str]) -> int:
    try:
        _, success_count = asyncio.run(correct_buildings_async(buildings_to_correct, city, 5, debug_log))
        return success_count
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            _, success_count = loop.run_until_complete(correct_buildings_async(buildings_to_correct, city, 5, debug_log))
            return success_count
        finally:
            loop.close()


def build_3d_map(request: ThreeDMapRequest) -> ThreeDMapResponse:
    warnings_list: list[str] = []
    debug_log: list[str] = []

    lat, lng, formatted_address = geocode_place(request.place_name, request.city_for_api)
    try:
        geojson = fetch_overture_buildings(lat, lng, request.radius_m)
    except Exception as exc:
        # Don't fail the whole endpoint if Overture is unavailable.
        # The frontend can still render Excel markers and will show the warning.
        warnings_list.append(f"Overture buildings unavailable: {exc}")
        geojson = {"type": "FeatureCollection", "features": []}
    custom_buildings = load_excel_buildings(get_excel_path(), warnings_list)

    # --- Early radius filter: only keep buildings near the search area ---
    # This avoids expensive geocoding / polygon-matching on the full dataset.
    if custom_buildings:
        total_loaded = len(custom_buildings)
        custom_buildings = [
            b for b in custom_buildings
            if haversine_distance(lat, lng, b["lat"], b["lng"]) <= request.radius_m
        ]
        debug_log.append(
            f"Radius filter: {len(custom_buildings)} of {total_loaded} buildings within {request.radius_m}m"
        )

    corrected_buildings = 0
    dry_run_estimated_corrections = 0
    if request.use_geocoding and custom_buildings:
        init_cache_db()
        city = (request.city_for_api or "").strip()
        if not city:
            parts = formatted_address.split(",")
            city = parts[1].strip() if len(parts) >= 2 else "India"

        overture_polygons = []
        for feat in geojson.get("features", []):
            geom = feat.get("geometry")
            if geom and geom.get("type") in ["Polygon", "MultiPolygon"]:
                try:
                    poly = shape(geom)
                    if poly.is_valid:
                        overture_polygons.append(poly)
                except Exception:
                    continue

        buildings_to_correct = []
        for building in custom_buildings:
            point = Point(building["lng"], building["lat"])
            already_matched = any(poly.covers(point) for poly in overture_polygons)
            if already_matched:
                debug_log.append(f"{building['name']}: already matched to Overture polygon, skipping geocoding")
                continue
            cached = get_cached_coordinates(building["name"], city)
            if cached:
                corrected_lat, corrected_lng, _ = cached
                building["lat"] = corrected_lat
                building["lng"] = corrected_lng
                debug_log.append(f"{building['name']}: using cached coordinates")
            else:
                buildings_to_correct.append(building)

        if request.dry_run:
            dry_run_estimated_corrections = len(buildings_to_correct)
            debug_log.append(f"Dry run: {dry_run_estimated_corrections} building(s) would be corrected")
        elif buildings_to_correct:
            corrected_buildings = _run_correction(buildings_to_correct, city, debug_log)

    markers = []
    for building in custom_buildings:
        if haversine_distance(lat, lng, building["lat"], building["lng"]) <= request.radius_m:
            markers.append({"lat": building["lat"], "lng": building["lng"], "name": building["name"]})

    source_counts = {"exact_match": 0, "snapped": 0, "unmatched": 0}
    if custom_buildings:
        _, source_counts, updated_features = annotate_custom_buildings_with_snapping(
            geojson["features"], custom_buildings, snap_distance_m=SNAP_DISTANCE_M
        )
        geojson["features"] = updated_features

    return ThreeDMapResponse(
        location=MapLocation(lat=lat, lng=lng, formatted_address=formatted_address),
        geojson=geojson,
        excel_markers=markers,
        warnings=warnings_list,
        debug_logs=debug_log if request.include_debug_logs else [],
        summary=MapSummary(
            total_excel_buildings=len(custom_buildings),
            exact_matches=source_counts["exact_match"],
            snapped_matches=source_counts["snapped"],
            unmatched=source_counts["unmatched"],
            visible_excel_markers=len(markers),
            overture_building_count=len(geojson.get("features", [])),
            corrected_buildings=corrected_buildings,
            dry_run_estimated_corrections=dry_run_estimated_corrections,
        ),
    )
