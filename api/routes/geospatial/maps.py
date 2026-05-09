from fastapi import APIRouter, HTTPException, Query

# from agents.geospatial.services.map_3d import build_3d_map
from api.schemas.geospatial.maps import (
    ThreeDMapRequest,
    ThreeDMapResponse,
    ThreeDMapTimelapseRequest,
    ThreeDMapTimelapseResponse,
    SpatialAnalysisRequest,
    SpatialAnalysisResponse,
    ProjectRateTimelapseRequest,
    ProjectRateTimelapseResponse,
    LocationRateTimelapseRequest,
    LocationRateTimelapseResponse,
    HeatmapTimelapseRequest,
    HeatmapTimelapseResponse,
)
from agents.geospatial.services.map_3d import build_3d_map
from agents.geospatial.services.map_3d_timelapse import build_3d_map_timelapse
from agents.geospatial.services.spatial_analysis import run_spatial_analysis
from agents.geospatial.services.project_rate_timelapse import build_project_rate_timelapse
from agents.geospatial.services.location_rate_timelapse import build_location_rate_timelapse
from agents.geospatial.services.heatmap_timelapse import build_heatmap_timelapse
from core.config import settings


router = APIRouter(prefix="/maps", tags=["maps"])


@router.post("/3d", response_model=ThreeDMapResponse)
def get_3d_map(payload: ThreeDMapRequest) -> ThreeDMapResponse:
    try:
        return build_3d_map(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/3d-timelapse", response_model=ThreeDMapTimelapseResponse)
def get_3d_map_timelapse(
    payload: ThreeDMapTimelapseRequest,
    debug_fill: bool = Query(False, description="Include fill_summary in response for debugging"),
) -> ThreeDMapTimelapseResponse:
    try:
        result = build_3d_map_timelapse(payload)
        if not debug_fill:
            result.summary.fill_summary = None
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/spatial-analysis", response_model=SpatialAnalysisResponse)
def get_spatial_analysis(payload: SpatialAnalysisRequest) -> SpatialAnalysisResponse:
    excel_path = settings.SPATIAL_ANALYSIS_EXCEL_PATH
    column_mapping_path = settings.COLUMN_MAPPING_PATH
    if not excel_path or not column_mapping_path:
        raise HTTPException(
            status_code=503,
            detail="SPATIAL_ANALYSIS_EXCEL_PATH or COLUMN_MAPPING_PATH is not configured in the backend .env file.",
        )
    try:
        result = run_spatial_analysis(
            user_query=payload.user_query,
            subject_name=payload.subject_name,
            subject_lat=payload.subject_lat,
            subject_lon=payload.subject_lon,
            use_subject=payload.use_subject,
            excel_path=excel_path,
            mapping_path=column_mapping_path,
        )
        return SpatialAnalysisResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/project-rate-growth", response_model=ProjectRateTimelapseResponse)
def get_project_rate_growth_timelapse(payload: ProjectRateTimelapseRequest) -> ProjectRateTimelapseResponse:
    try:
        return build_project_rate_timelapse(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/location-rate-volume", response_model=LocationRateTimelapseResponse)
def get_location_rate_volume_timelapse(payload: LocationRateTimelapseRequest) -> LocationRateTimelapseResponse:
    try:
        return build_location_rate_timelapse(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/heatmap-timelapse", response_model=HeatmapTimelapseResponse)
def get_heatmap_timelapse(payload: HeatmapTimelapseRequest) -> HeatmapTimelapseResponse:
    try:
        return build_heatmap_timelapse(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

