from pydantic import BaseModel
from typing import List, Optional


class VillagesForCityResponse(BaseModel):
    villages: List[str]


class PriceMomentumItem(BaseModel):
    project_id: int
    project_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    village_names: Optional[str] = None
    year_latest: Optional[int] = None
    year_previous: Optional[int] = None
    avg_ca_latest: Optional[float] = None
    avg_sa_latest: Optional[float] = None
    avg_ca_previous: Optional[float] = None
    avg_sa_previous: Optional[float] = None
    growth_pct_ca: Optional[float] = None
    growth_pct_sa: Optional[float] = None
    city_id: Optional[int] = None
