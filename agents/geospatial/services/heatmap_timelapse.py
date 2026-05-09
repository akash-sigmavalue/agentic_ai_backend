import math
from typing import Any
from shapely.geometry import shape

from api.schemas.geospatial.maps import (
    HeatmapTimelapseRequest,
    HeatmapTimelapseResponse,
    HeatmapSummary,
    HeatmapHub
)
from agents.geospatial.services.map_3d import geocode_place, fetch_overture_buildings

# Mock data for Phase 1
MOCK_DATES = ["2023-Q1", "2023-Q2", "2023-Q3", "2023-Q4"]

# Hubs in Pune (roughly)
MOCK_HUBS = [
    HeatmapHub(name="Koregaon Park", lat=18.5362, lng=73.8939, rates=[18000, 18500, 19000, 19500]),
    HeatmapHub(name="Aundh", lat=18.5580, lng=73.8075, rates=[12000, 12200, 12500, 12800]),
    HeatmapHub(name="Kothrud", lat=18.5074, lng=73.8077, rates=[14000, 14300, 14500, 14800]),
    HeatmapHub(name="Viman Nagar", lat=18.5679, lng=73.9143, rates=[13500, 13800, 14200, 14500]),
]

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0 # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def calculate_idw(target_lat: float, target_lng: float, hubs: list[HeatmapHub], date_idx: int, power: int = 2) -> float:
    numerator = 0.0
    denominator = 0.0
    
    for hub in hubs:
        dist = haversine(target_lat, target_lng, hub.lat, hub.lng)
        # Avoid division by zero
        if dist < 0.01:
            return hub.rates[date_idx]
            
        weight = 1.0 / (dist ** power)
        numerator += weight * hub.rates[date_idx]
        denominator += weight
        
    if denominator == 0:
        return 0.0
    return numerator / denominator


def build_heatmap_timelapse(request: HeatmapTimelapseRequest) -> HeatmapTimelapseResponse:
    # 1. Geocode
    lat, lng, formatted_address = geocode_place(request.place_name, request.city_for_api)
    
    # 2. Fetch Buildings
    try:
        geojson = fetch_overture_buildings(lat, lng, request.radius_m)
    except Exception as exc:
        geojson = {"type": "FeatureCollection", "features": []}

    features = geojson.get("features", [])
    
    # 3. Calculate Global Min/Max from Hubs
    all_rates = [r for hub in MOCK_HUBS for r in hub.rates]
    global_min = min(all_rates) if all_rates else 0
    global_max = max(all_rates) if all_rates else 1
    
    # 4. Interpolate rates for each building
    for feature in features:
        geom = feature.get("geometry")
        if not geom:
            continue
            
        try:
            poly = shape(geom)
            centroid = poly.centroid
            b_lat, b_lng = centroid.y, centroid.x
            
            # Use a default height if none exists
            height = feature.get("properties", {}).get("height", 15)
            if feature.get("properties") is None:
                feature["properties"] = {}
                
            feature["properties"]["height_render"] = height
            
            interpolated_rates = []
            for i in range(len(MOCK_DATES)):
                rate = calculate_idw(b_lat, b_lng, MOCK_HUBS, i)
                interpolated_rates.append(rate)
                
            feature["properties"]["interpolated_rates"] = interpolated_rates
            
        except Exception:
            pass

    return HeatmapTimelapseResponse(
        dates=MOCK_DATES,
        geojson=geojson,
        hubs=MOCK_HUBS,
        summary=HeatmapSummary(
            global_min_rate=global_min,
            global_max_rate=global_max,
            overture_building_count=len(features)
        )
    )
