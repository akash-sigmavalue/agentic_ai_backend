from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database.portfolio.db import get_db
from services.portfolio.dashboard_service import dashboard
from services.portfolio.derived_calculation_service import refresh_derived_records
from services.portfolio.portfolio_flat_service import refresh_portfolio_flat_records

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    return dashboard(db)


@router.post("/dashboard/refresh")
def refresh_dashboard(db: Session = Depends(get_db)):
    refresh_derived_records(db)
    refresh_portfolio_flat_records(db)
    return dashboard(db)
