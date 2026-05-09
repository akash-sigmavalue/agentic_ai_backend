import os
from pathlib import Path
from typing import Any

import pandas as pd
from shapely.geometry import Point, shape

from agents.geospatial.services.floor_rate_fallback import fill_floor_rates
from core.config import settings
from api.schemas.geospatial.maps import (
    MapLocation,
    ThreeDMapTimelapseRequest,
    ThreeDMapTimelapseResponse,
    ThreeDMapTimelapseSummary,
)
from agents.geospatial.services.map_3d import (
    FLOOR_HEIGHT_M,
    _parse_floor_number,
    fetch_overture_buildings,
    geocode_place,
    get_cached_coordinates,
    get_excel_path,
    haversine_distance,
    init_cache_db,
    match_custom_buildings_to_features,
    _run_correction,
)


def get_timelapse_excel_path() -> Path:
    if settings.THREE_D_MAPS_TIMELAPSE_EXCEL_PATH:
        return Path(settings.THREE_D_MAPS_TIMELAPSE_EXCEL_PATH)
    if settings.THREE_D_MAPS_EXCEL_PATH:
        return Path(settings.THREE_D_MAPS_EXCEL_PATH)
    return get_excel_path()


def load_timeseries_excel(
    excel_path: str | os.PathLike[str],
    warnings_list: list[str],
) -> tuple[list[dict[str, Any]], list[str], float, float]:
    if not os.path.exists(excel_path):
        warnings_list.append(f"Excel file not found at {excel_path}. No custom buildings.")
        return [], [], 0, 1

    try:
        dataframe = pd.read_excel(excel_path)
        transaction_cols = {
            "project_name",
            "project_latitude",
            "project_longitude",
            "floor_number",
            "agreement_price",
            "net_carpet_area_sq_m",
            "transaction_date",
        }
        legacy_cols = {"Name", "lat", "lng", "floors", "current floor", "date", "rate"}

        is_transaction_schema = transaction_cols.issubset(dataframe.columns)
        is_legacy_schema = legacy_cols.issubset(dataframe.columns)
        if not is_transaction_schema and not is_legacy_schema:
            warnings_list.append(
                "Excel must contain either "
                f"{transaction_cols} or {legacy_cols}. Found: {list(dataframe.columns)}"
            )
            return [], [], 0, 1

        date_col = "transaction_date" if is_transaction_schema else "date"
        dataframe[date_col] = pd.to_datetime(dataframe[date_col], errors="coerce")
        dataframe = dataframe.dropna(subset=[date_col])
        if dataframe.empty:
            warnings_list.append(f"No valid {date_col} rows found in timelapse Excel.")
            return [], [], 0, 1

        month_labels = sorted(dataframe[date_col].dt.to_period("M").astype(str).unique().tolist())
        monthly_avg_rates: list[float] = []
        buildings: list[dict[str, Any]] = []

        grouped: dict[str, dict[str, Any]] = {}
        skipped = 0
        for _, row in dataframe.iterrows():
            try:
                raw_name = row["project_name"] if is_transaction_schema else row["Name"]
                if pd.isna(raw_name) or str(raw_name).strip() == "":
                    skipped += 1
                    continue
                name = str(raw_name).strip()

                lat_col = "project_latitude" if is_transaction_schema else "lat"
                lng_col = "project_longitude" if is_transaction_schema else "lng"
                if pd.isna(row[lat_col]) or pd.isna(row[lng_col]):
                    continue
                lat = float(row[lat_col])
                lng = float(row[lng_col])

                floor_col = "floor_number" if is_transaction_schema else "current floor"
                floor_num = _parse_floor_number(row[floor_col])
                if floor_num is None:
                    continue

                if is_transaction_schema:
                    agreement_price = row["agreement_price"]
                    carpet_area = row["net_carpet_area_sq_m"]
                    if pd.isna(agreement_price) or pd.isna(carpet_area) or float(carpet_area) <= 0:
                        continue
                    rate = float(agreement_price) / float(carpet_area)
                else:
                    if pd.isna(row["rate"]):
                        continue
                    rate = float(row["rate"])

                month_label = pd.Timestamp(row[date_col]).to_period("M").strftime("%Y-%m")

                bucket = grouped.setdefault(
                    name,
                    {
                        "latitudes": [],
                        "longitudes": [],
                        "entries": [],
                    },
                )
                bucket["latitudes"].append(lat)
                bucket["longitudes"].append(lng)
                bucket["entries"].append((floor_num, month_label, rate))
            except Exception as exc:
                warnings_list.append(f"Skipping row: {exc}")

        if skipped > 0:
            warnings_list.append(f"Skipped {skipped} row(s) with null/empty project_name.")

        for name, project_bucket in grouped.items():
            entries: list[tuple[int, str, float]] = project_bucket["entries"]
            if not entries:
                continue

            all_floors = [floor_num for floor_num, _, _ in entries]
            min_floor = min(all_floors)
            max_floor = max(all_floors)
            total_floors = max(max_floor - min_floor + 1, 1)

            floor_month_rate_accum: dict[tuple[int, str], list[float]] = {}
            for floor_num, month_label, rate in entries:
                floor_idx = floor_num - min_floor
                if 0 <= floor_idx < total_floors:
                    floor_month_rate_accum.setdefault((floor_idx, month_label), []).append(rate)

            floor_rates_by_date: list[list[float | None]] = []
            for _month_label in month_labels:
                month_rates: list[float | None] = [None] * total_floors
                for floor_idx in range(total_floors):
                    rates = floor_month_rate_accum.get((floor_idx, _month_label), [])
                    if rates:
                        avg_rate = sum(rates) / len(rates)
                        month_rates[floor_idx] = avg_rate
                        monthly_avg_rates.append(avg_rate)
                floor_rates_by_date.append(month_rates)

            if not any(any(rate is not None for rate in month_rates) for month_rates in floor_rates_by_date):
                warnings_list.append(f"Building {name} has no rate data. Skipping.")
                continue

            latitudes = project_bucket["latitudes"]
            longitudes = project_bucket["longitudes"]
            avg_lat = sum(latitudes) / len(latitudes)
            avg_lng = sum(longitudes) / len(longitudes)

            buildings.append(
                {
                    "name": name,
                    "lat": avg_lat,
                    "lng": avg_lng,
                    "total_floors": total_floors,
                    "floor_rates_by_date": floor_rates_by_date,
                }
            )

        global_min = min(monthly_avg_rates) if monthly_avg_rates else 0
        global_max = max(monthly_avg_rates) if monthly_avg_rates else 1
        return buildings, month_labels, global_min, global_max
    except Exception as exc:
        warnings_list.append(f"Failed to read Excel file: {exc}")
        return [], [], 0, 1


def _mean_floor_rates_across_months(building: dict[str, Any]) -> list[float | None]:
    """One row per floor: mean of that floor's monthly averages (for min/max + floor_rates parity)."""
    frbd = building.get("floor_rates_by_date") or []
    total = int(building.get("total_floors") or 0)
    if total <= 0:
        return []
    acc: list[list[float]] = [[] for _ in range(total)]
    for month_rates in frbd:
        if not isinstance(month_rates, list):
            continue
        for i, r in enumerate(month_rates):
            if i < total and r is not None:
                acc[i].append(float(r))
    return [sum(vals) / len(vals) if vals else None for vals in acc]


def apply_timelapse_annotations_to_features(
    features: list[dict[str, Any]],
    matched_indices: dict[int, dict[str, Any]],
    dates: list[str],
    global_min: float,
    global_max: float,
) -> None:
    for idx, building in matched_indices.items():
        floor_rates_snapshot = _mean_floor_rates_across_months(building)
        existing_rates = [r for r in floor_rates_snapshot if r is not None]
        min_rate = min(existing_rates) if existing_rates else 0
        max_rate = max(existing_rates) if existing_rates else 1
        features[idx]["properties"]["is_custom"] = True
        features[idx]["properties"]["building_name"] = building["name"]
        features[idx]["properties"]["num_floors"] = building["total_floors"]
        features[idx]["properties"]["height_render"] = building["total_floors"] * FLOOR_HEIGHT_M
        features[idx]["properties"]["floor_rates"] = floor_rates_snapshot
        features[idx]["properties"]["min_rate"] = min_rate
        features[idx]["properties"]["max_rate"] = max_rate
        features[idx]["properties"]["floor_rates_by_date"] = building["floor_rates_by_date"]
        features[idx]["properties"]["floor_rates_enriched"] = building.get("floor_rates_enriched")
        features[idx]["properties"]["fill_summary"] = building.get("fill_summary")
        features[idx]["properties"]["dates"] = dates
        features[idx]["properties"]["global_min_rate"] = global_min
        features[idx]["properties"]["global_max_rate"] = global_max


def build_3d_map_timelapse(request: ThreeDMapTimelapseRequest) -> ThreeDMapTimelapseResponse:
    warnings_list: list[str] = []
    debug_log: list[str] = []

    lat, lng, formatted_address = geocode_place(request.place_name, request.city_for_api)
    try:
        geojson = fetch_overture_buildings(lat, lng, request.radius_m)
    except Exception as exc:
        warnings_list.append(f"Overture buildings unavailable: {exc}")
        geojson = {"type": "FeatureCollection", "features": []}

    excel_path = get_timelapse_excel_path()
    debug_log.append(f"Timelapse Excel source: {excel_path}")
    custom_buildings, dates, global_min, global_max = load_timeseries_excel(excel_path, warnings_list)

    # --- Fallback fill: enrich every building's floor rates ---
    aggregate_fill_summary: dict[str, int] = {}
    if custom_buildings and dates:
        all_filled_rates: list[float] = []
        for building in custom_buildings:
            building["_dates"] = dates  # inject dates for fallback module
            filled = fill_floor_rates(building)
            # Copy enriched data back onto the original building dict
            building["floor_rates_by_date"] = filled["floor_rates_by_date"]
            building["floor_rates_enriched"] = filled.get("floor_rates_enriched", [])
            building["fill_summary"] = filled.get("fill_summary", {})
            # Collect all non-null rates for global min/max recalculation
            for month_row in building["floor_rates_by_date"]:
                for r in month_row:
                    if r is not None:
                        all_filled_rates.append(r)
            # Aggregate summaries
            for key, val in building.get("fill_summary", {}).items():
                aggregate_fill_summary[key] = aggregate_fill_summary.get(key, 0) + val
        # Recalculate global rate bounds after fill
        if all_filled_rates:
            global_min = min(all_filled_rates)
            global_max = max(all_filled_rates)
        debug_log.append(f"Fallback fill complete: {aggregate_fill_summary}")

    if custom_buildings:
        total_loaded = len(custom_buildings)
        custom_buildings = [
            building
            for building in custom_buildings
            if haversine_distance(lat, lng, building["lat"], building["lng"]) <= request.radius_m
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
        matched_indices, source_tracker = match_custom_buildings_to_features(
            geojson["features"], custom_buildings
        )
        apply_timelapse_annotations_to_features(
            geojson["features"], matched_indices, dates, global_min, global_max
        )
        source_counts = {
            "exact_match": source_tracker.count("exact_match"),
            "snapped": source_tracker.count("snapped"),
            "unmatched": source_tracker.count("unmatched"),
        }

    unmatched = max(0, len(custom_buildings) - source_counts["exact_match"] - source_counts["snapped"])

    return ThreeDMapTimelapseResponse(
        location=MapLocation(lat=lat, lng=lng, formatted_address=formatted_address),
        geojson=geojson,
        excel_markers=markers,
        dates=dates,
        warnings=warnings_list,
        debug_logs=debug_log if request.include_debug_logs else [],
        summary=ThreeDMapTimelapseSummary(
            total_excel_buildings=len(custom_buildings),
            exact_matches=source_counts["exact_match"],
            snapped_matches=source_counts["snapped"],
            unmatched=unmatched,
            visible_excel_markers=len(markers),
            overture_building_count=len(geojson.get("features", [])),
            time_steps=len(dates),
            global_min_rate=global_min,
            global_max_rate=global_max,
            corrected_buildings=corrected_buildings,
            dry_run_estimated_corrections=dry_run_estimated_corrections,
            fill_summary=aggregate_fill_summary if aggregate_fill_summary else None,
        ),
    )
