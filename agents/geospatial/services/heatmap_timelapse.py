import math
from shapely.geometry import shape

from api.schemas.geospatial.maps import (
    HeatmapTimelapseRequest,
    HeatmapTimelapseResponse,
    HeatmapSummary,
    HeatmapHub,
)
from agents.geospatial.services.map_3d import (
    geocode_place,
    fetch_overture_buildings,
    fetch_overture_buildings_for_focus_points,
)
from agents.geospatial.services.map_3d_timelapse import (
    get_timelapse_excel_path,
    load_timeseries_excel,
)

# ── Fallback mock data (used only when Excel is unavailable or empty) ─────────
_MOCK_DATES = ["2023-Q1", "2023-Q2", "2023-Q3", "2023-Q4"]
_MOCK_HUBS = [
    HeatmapHub(name="Koregaon Park", lat=18.5362, lng=73.8939, rates=[18000, 18500, 19000, 19500]),
    HeatmapHub(name="Aundh",         lat=18.5580, lng=73.8075, rates=[12000, 12200, 12500, 12800]),
    HeatmapHub(name="Kothrud",       lat=18.5074, lng=73.8077, rates=[14000, 14300, 14500, 14800]),
    HeatmapHub(name="Viman Nagar",   lat=18.5679, lng=73.9143, rates=[13500, 13800, 14200, 14500]),
]

# Maximum km radius from the search centre in which to collect real data hubs.
# Wide enough for good IDW coverage; buildings beyond this are ignored.
_HUB_RADIUS_KM = 10.0


# ── Math helpers ──────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in kilometres."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_idw(
    target_lat: float,
    target_lng: float,
    hubs: list[HeatmapHub],
    date_idx: int,
    power: int = 2,
) -> float:
    """Inverse-distance-weighted interpolation of hub rates at a target point.
    Hubs with a zero rate for this time step are skipped (zero = no real data).
    """
    numerator = 0.0
    denominator = 0.0
    for hub in hubs:
        hub_rate = hub.rates[date_idx]
        if hub_rate <= 0:          # skip zero-filled / missing months
            continue
        dist = _haversine_km(target_lat, target_lng, hub.lat, hub.lng)
        if dist < 0.01:            # target sits on top of a hub
            return hub_rate
        weight = 1.0 / (dist ** power)
        numerator += weight * hub_rate
        denominator += weight
    return numerator / denominator if denominator else 0.0


# ── None-gap filler ───────────────────────────────────────────────────────────

def _fill_none_rates(rates: list[float | None]) -> list[float]:
    """Forward-fill then backward-fill None slots; any remaining become 0.0."""
    result: list[float | None] = list(rates)
    # forward pass
    last: float | None = None
    for i, r in enumerate(result):
        if r is not None:
            last = r
        elif last is not None:
            result[i] = last
    # backward pass (handles leading Nones)
    last = None
    for i in range(len(result) - 1, -1, -1):
        if result[i] is not None:
            last = result[i]
        elif last is not None:
            result[i] = last
    return [r if r is not None else 0.0 for r in result]


# ── Hub builder ───────────────────────────────────────────────────────────────

def _build_hubs_from_excel(
    center_lat: float,
    center_lng: float,
    warnings_list: list[str],
) -> tuple[list[HeatmapHub], list[str]]:
    """
    Load real building data from the timelapse Excel and convert to HeatmapHub
    objects.  Each hub's rate per time period is the **average across all floors**
    for that month — no floor-level breakdown needed for this view.

    Returns (hubs, dates).  On error returns ([], []).
    """
    try:
        excel_path = get_timelapse_excel_path()
    except ValueError as exc:
        warnings_list.append(f"Timelapse Excel path not configured: {exc}")
        return [], []

    buildings, dates, _, _ = load_timeseries_excel(excel_path, warnings_list)

    if not buildings or not dates:
        warnings_list.append("No building data returned from timelapse Excel.")
        return [], []

    hubs: list[HeatmapHub] = []

    for building in buildings:
        b_lat: float = building["lat"]
        b_lng: float = building["lng"]

        # Only include hubs within the configured radius of the search centre.
        dist_km = _haversine_km(center_lat, center_lng, b_lat, b_lng)
        if dist_km > _HUB_RADIUS_KM:
            continue

        floor_rates_by_date: list[list[float | None]] = building.get(
            "floor_rates_by_date", []
        )
        if not floor_rates_by_date:
            continue

        # Average all floor rates → one value per time step
        monthly_avg: list[float | None] = []
        for month_rates in floor_rates_by_date:
            valid = [r for r in month_rates if r is not None]
            monthly_avg.append(sum(valid) / len(valid) if valid else None)

        # Skip buildings with no rate data whatsoever
        if not any(r is not None for r in monthly_avg):
            continue

        filled = _fill_none_rates(monthly_avg)

        hubs.append(
            HeatmapHub(
                name=building["name"],
                lat=b_lat,
                lng=b_lng,
                rates=filled,
            )
        )

    # Sort nearest-first so hubs[0] is used correctly by the frontend
    # for map auto-centering.
    hubs.sort(
        key=lambda h: _haversine_km(center_lat, center_lng, h.lat, h.lng)
    )

    return hubs, dates


# ── Main entry point ──────────────────────────────────────────────────────────

def build_heatmap_timelapse(request: HeatmapTimelapseRequest) -> HeatmapTimelapseResponse:
    warnings_list: list[str] = []

    focus_points = [
        (point.lat, point.lng)
        for point in (request.focus_points or [])
        if math.isfinite(point.lat) and math.isfinite(point.lng)
    ]

    # 1. Use explicit Module 2 focus points when supplied; otherwise geocode the searched location.
    if focus_points:
        lat = sum(point[0] for point in focus_points) / len(focus_points)
        lng = sum(point[1] for point in focus_points) / len(focus_points)
    else:
        lat, lng, _formatted_address = geocode_place(request.place_name, request.city_for_api)

    # 2. Fetch Overture building footprints within the requested radius
    try:
        if focus_points:
            per_location_cap = request.max_buildings_per_location or (300 if request.fast_mode else None)
            total_cap = request.max_total_buildings or (1000 if request.fast_mode else None)
            geojson = fetch_overture_buildings_for_focus_points(
                focus_points,
                request.radius_m,
                max_buildings_per_location=per_location_cap,
                max_total_buildings=total_cap,
            )
        else:
            geojson = fetch_overture_buildings(
                lat,
                lng,
                request.radius_m,
                max_buildings=request.max_total_buildings if request.fast_mode else None,
                true_radius_filter=request.fast_mode,
            )
    except Exception as exc:
        warnings_list.append(f"Overture buildings unavailable: {exc}")
        geojson = {"type": "FeatureCollection", "features": []}

    features = geojson.get("features", [])

    # 3. Load real hub data from Excel
    hubs, dates = _build_hubs_from_excel(lat, lng, warnings_list)

    # 4. Fall back to mock if Excel produced nothing usable
    if not hubs or not dates:
        warnings_list.append("No real hub data available — using mock rate data.")
        hubs = _MOCK_HUBS
        dates = _MOCK_DATES

    # 5. Global rate bounds — exclude zero-filled slots (no real data)
    all_rates = [r for hub in hubs for r in hub.rates if r > 0]
    global_min = min(all_rates) if all_rates else 0.0
    global_max = max(all_rates) if all_rates else 1.0

    # 6. IDW-interpolate a rate for every Overture building at every time step
    for feature in features:
        geom = feature.get("geometry")
        if not geom:
            continue
        try:
            poly = shape(geom)
            centroid = poly.centroid
            b_lat, b_lng = centroid.y, centroid.x

            height = (feature.get("properties") or {}).get("height", 15)
            if feature.get("properties") is None:
                feature["properties"] = {}
            feature["properties"]["height_render"] = height

            feature["properties"]["interpolated_rates"] = [
                calculate_idw(b_lat, b_lng, hubs, i)
                for i in range(len(dates))
            ]
        except Exception:
            pass

    return HeatmapTimelapseResponse(
        dates=dates,
        geojson=geojson,
        hubs=hubs,
        summary=HeatmapSummary(
            global_min_rate=global_min,
            global_max_rate=global_max,
            overture_building_count=len(features),
        ),
    )
