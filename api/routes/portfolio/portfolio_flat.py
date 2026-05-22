from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database.portfolio.db import get_db
from services.portfolio.portfolio_flat_service import refresh_portfolio_flat_records

router = APIRouter(tags=["portfolio-flat"])


@router.post("/portfolio-flat/refresh")
def refresh_portfolio_flat(db: Session = Depends(get_db)):
    refresh_portfolio_flat_records(db)
    return {"status": "success", "message": "portfolio_flat_records refreshed"}
