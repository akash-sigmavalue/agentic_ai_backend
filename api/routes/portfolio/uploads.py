from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from api.schemas.portfolio.upload import MappingUpdateRequest
from database.portfolio.db import get_db
from services.portfolio.upload_service import confirm_upload, mapping_payload, patch_upload_mapping, preview_global_upload, preview_section_upload

router = APIRouter(tags=["uploads"])


@router.post("/uploads/global/preview")
def global_upload_preview(file: UploadFile = File(...), db: Session = Depends(get_db)):
    return preview_global_upload(file, db)


@router.patch("/uploads/global/{upload_id}/mapping")
def patch_global_mapping(upload_id: int, payload: MappingUpdateRequest, db: Session = Depends(get_db)):
    return patch_upload_mapping(upload_id, payload.model_dump(), db)


@router.post("/uploads/global/{upload_id}/confirm")
def confirm_global_upload(upload_id: int, db: Session = Depends(get_db)):
    return confirm_upload(upload_id, db)


@router.post("/uploads/{section_key}/preview")
def section_upload_preview(section_key: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    return preview_section_upload(section_key, file, db)


@router.post("/uploads/{section_key}/auto-import")
def section_auto_import(section_key: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    preview = preview_section_upload(section_key, file, db)
    return {**confirm_upload(preview["upload_id"], db), "preview": preview}


@router.get("/uploads/{upload_id}/mapping")
def get_mapping(upload_id: int, db: Session = Depends(get_db)):
    return mapping_payload(upload_id, db)


@router.patch("/uploads/{upload_id}/mapping")
def patch_mapping(upload_id: int, payload: MappingUpdateRequest, db: Session = Depends(get_db)):
    return patch_upload_mapping(upload_id, payload.model_dump(), db)


@router.post("/uploads/{upload_id}/confirm")
def confirm(upload_id: int, db: Session = Depends(get_db)):
    return confirm_upload(upload_id, db)
