from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ThreeDMapRequest(BaseModel):
    place_name: str = Field(..., min_length=2, description="Location to geocode and inspect")
    radius_m: int = Field(default=450, ge=150, le=1000)
    use_geocoding: bool = Field(default=False, description="Correct unmatched Excel coordinates")
    city_for_api: str | None = Field(default=None, description="Optional city hint for coordinate correction")
    dry_run: bool = Field(default=False, description="Estimate correction work without updating coordinates")
    include_debug_logs: bool = Field(default=False, description="Return verbose geocoding debug logs")


class MapLocation(BaseModel):
    lat: float
    lng: float
    formatted_address: str


class MapSummary(BaseModel):
    total_excel_buildings: int
    exact_matches: int
    snapped_matches: int
    unmatched: int
    visible_excel_markers: int
    overture_building_count: int
    corrected_buildings: int
    dry_run_estimated_corrections: int = 0


class ThreeDMapResponse(BaseModel):
    location: MapLocation
    geojson: dict[str, Any]
    excel_markers: list[dict[str, Any]]
    warnings: list[str]
    debug_logs: list[str]
    summary: MapSummary


class ThreeDMapTimelapseRequest(BaseModel):
    place_name: str = Field(..., min_length=2, description="Location to geocode and inspect")
    radius_m: int = Field(default=450, ge=150, le=1000)
    use_geocoding: bool = Field(default=False, description="Correct unmatched Excel coordinates (same as 3D map)")
    city_for_api: str | None = Field(default=None, description="Optional city hint for coordinate correction / geocoding")
    dry_run: bool = Field(default=False, description="Estimate correction work without updating coordinates")
    include_debug_logs: bool = Field(default=False, description="Return verbose processing logs")


class ThreeDMapTimelapseSummary(BaseModel):
    total_excel_buildings: int
    exact_matches: int
    snapped_matches: int
    unmatched: int
    visible_excel_markers: int
    overture_building_count: int
    time_steps: int
    global_min_rate: float
    global_max_rate: float
    corrected_buildings: int = 0
    dry_run_estimated_corrections: int = 0
    fill_summary: dict[str, Any] | None = None


class ThreeDMapTimelapseResponse(BaseModel):
    location: MapLocation
    geojson: dict[str, Any]
    excel_markers: list[dict[str, Any]]
    dates: list[str]
    warnings: list[str]
    debug_logs: list[str]
    summary: ThreeDMapTimelapseSummary


# =========================================================
# Spatial Analysis (Map Visualization)
# =========================================================

class SpatialAnalysisRequest(BaseModel):
    user_query: str = Field(
        default=(
            "Analyze the relationship between road proximity and project rates for the plotted projects. "
            "Use the Excel data and extract only the required data from OSM map context. "
            "Make a step-by-step implementation plan, run the calculations, update the map overlay, "
            "and generate insights."
        ),
        description="The natural-language analysis request",
    )
    subject_name: str = Field(default="My Subject Project", description="Name of the optional subject project")
    subject_lat: float | None = Field(default=None, description="Latitude of the optional subject project")
    subject_lon: float | None = Field(default=None, description="Longitude of the optional subject project")
    use_subject: bool = Field(default=False, description="Whether to include the subject project in analysis")


class SpatialAnalysisStats(BaseModel):
    project_count: int
    road_count: int
    place_count: int
    total_cost_usd: float


class SpatialAnalysisResponse(BaseModel):
    projects: list[dict[str, Any]]
    roads: list[dict[str, Any]]
    places: list[dict[str, Any]]
    planner: dict[str, Any]
    execution_summary: dict[str, Any]
    insights: str
    subject_info: dict[str, Any] | None
    token_log: list[dict[str, Any]]
    progress_log: list[str]
    excel_preview: list[dict[str, Any]]
    stats: SpatialAnalysisStats


# =========================================================
# Project Rate + Growth Velocity Timelapse
# =========================================================

class ProjectRateTimelapseRequest(BaseModel):
    search: str | None = Field(default=None, description="Free-text search across project/location/city/micro_market")
    project_name: str | None = None
    tower_name: str | None = None
    property_type: str | None = None
    unit_configuration: str | None = None
    sale_type: str | None = None
    start_month: str | None = Field(default=None, description="YYYY-MM inclusive start")
    end_month: str | None = Field(default=None, description="YYYY-MM inclusive end")


class FloorMonthValue(BaseModel):
    rate_psf: float | None
    mom_growth_pct: float | None
    txn_count: int
    confidence_score: float
    fallback_level: int
    is_estimated: bool


class FloorTimelapse(BaseModel):
    floor_index: int
    monthly_values: dict[str, FloorMonthValue]


class BuildingTimelapse(BaseModel):
    project_id: str | None = None
    project_name: str
    tower_name: str | None = None
    latitude: float
    longitude: float
    floors: list[FloorTimelapse]


class ProjectRateTimelapseResponse(BaseModel):
    type: str = "project_rate_growth_timelapse"
    map_center: list[float]
    timeline: list[str]
    buildings: list[BuildingTimelapse]
    warnings: list[str]
    available_projects: list[str]
    available_towers: list[str]
    global_min_rate: float = 0.0
    global_max_rate: float = 1.0


# =========================================================
# Location Rate Heatmap + Volume Pulse Timelapse
# =========================================================

class LocationRateTimelapseRequest(BaseModel):
    search: str | None = Field(default=None, description="Free-text search across location/micro_market/city")
    location_name: str | None = None
    micro_market: str | None = None
    property_type: str | None = None
    unit_configuration: str | None = None
    sale_type: str | None = None
    start_month: str | None = Field(default=None, description="YYYY-MM inclusive start")
    end_month: str | None = Field(default=None, description="YYYY-MM inclusive end")


class LocationMonthValue(BaseModel):
    median_rate_psf: float | None
    avg_rate_psf: float | None
    transaction_volume: int
    active_project_count: int
    total_agreement_value: float
    rate_growth_pct: float | None
    volume_growth_pct: float | None
    momentum_score: float | None


class LocationTimelapse(BaseModel):
    location_name: str
    micro_market: str | None = None
    latitude: float
    longitude: float
    monthly_values: dict[str, LocationMonthValue]


class LocationRateTimelapseResponse(BaseModel):
    type: str = "location_rate_volume_timelapse"
    map_center: list[float]
    timeline: list[str]
    locations: list[LocationTimelapse]
    warnings: list[str]
    available_locations: list[str]
    available_micro_markets: list[str]
    global_min_rate: float = 0.0
    global_max_rate: float = 1.0


# =========================================================
# Location Heatmap Timelapse (IDW Interpolation)
# =========================================================

class HeatmapTimelapseRequest(BaseModel):
    place_name: str = Field(..., min_length=2, description="Location to geocode and inspect")
    radius_m: int = Field(default=450, ge=150, le=2000)
    city_for_api: str | None = Field(default=None, description="Optional city hint")


class HeatmapHub(BaseModel):
    name: str
    lat: float
    lng: float
    rates: list[float]


class HeatmapSummary(BaseModel):
    global_min_rate: float
    global_max_rate: float
    overture_building_count: int


class HeatmapTimelapseResponse(BaseModel):
    dates: list[str]
    geojson: dict[str, Any]
    hubs: list[HeatmapHub]
    summary: HeatmapSummary

