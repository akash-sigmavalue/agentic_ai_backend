from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.schemas.portfolio.record import RecordUpsertRequest
from database.portfolio.db import get_db
from services.portfolio.record_service import create_section_record, delete_section_record, list_section_records, update_section_record

router = APIRouter(tags=["records"])


@router.get("/records/{section_key}")
def get_records(section_key: str, db: Session = Depends(get_db)):
    return list_section_records(section_key, db)


@router.post("/records/{section_key}")
def create_record(section_key: str, payload: RecordUpsertRequest, db: Session = Depends(get_db)):
    return create_section_record(section_key, payload.model_dump(), db)


@router.patch("/records/{section_key}/{record_id}")
def update_record(section_key: str, record_id: int, payload: RecordUpsertRequest, db: Session = Depends(get_db)):
    return update_section_record(section_key, record_id, payload.model_dump(), db)


@router.delete("/records/{section_key}/{record_id}")
def delete_record(section_key: str, record_id: int, db: Session = Depends(get_db)):
    return delete_section_record(section_key, record_id, db)
