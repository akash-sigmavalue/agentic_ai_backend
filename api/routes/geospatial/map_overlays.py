from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any

from api.schemas.geospatial.map_overlays import VillagesForCityResponse, PriceMomentumItem
from agents.geospatial.services.map_overlay_service import (
    get_villages_for_city,
    get_price_momentum,
)

router = APIRouter(prefix="/map-overlays", tags=["map-overlays"])


@router.get("/villages-for-city", response_model=VillagesForCityResponse)
def villages_for_city(city_id: int = Query(..., description="City ID to filter villages")):
    """Return distinct village names for a given city."""
    try:
        villages = get_villages_for_city(city_id)
        return VillagesForCityResponse(villages=villages)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/price-momentum", response_model=List[Dict[str, Any]])
def price_momentum(
    city_id: Optional[int] = Query(None),
    project_id: Optional[int] = Query(None),
    village_name: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
):
    """
    Return price momentum data grouped by project.

    Shows latest vs previous year rates and growth percentages
    for both carpet area and salable area.
    """
    try:
        data = get_price_momentum(
            city_id=city_id,
            project_id=project_id,
            village_name=village_name,
            year=year,
        )
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
