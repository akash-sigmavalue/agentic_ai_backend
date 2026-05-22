from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.portfolio.repositories import record_repository
from registry.portfolio.registry import get_section
from services.portfolio.asset_section_service import ensure_asset_section_shell_records
from services.portfolio.dashboard_service import dashboard_rows
from services.portfolio.derived_calculation_service import derived_audit_with_summary, refresh_derived_records, split_record_by_category
from services.portfolio.portfolio_flat_service import refresh_portfolio_flat_records
from tools.portfolio.validation import validate_record


def serialize_record(row) -> dict:
    return {
        "id": row.id,
        "asset_id": row.asset_id,
        "record_data": row.record_data or {},
        "custom_fields": row.custom_fields or {},
        "derived_audit": derived_audit_with_summary(row.derived_audit),
        "validation_status": row.validation_status,
        "validation_errors": row.validation_errors or [],
    }


def list_section_records(section_key: str, db: Session) -> dict:
    get_section(section_key)
    if section_key == "dashboard":
        records = [
            {
                "id": index + 1,
                "asset_id": row.get("assetId"),
                "record_data": row,
                "custom_fields": {},
                "validation_status": "valid",
                "validation_errors": [],
            }
            for index, row in enumerate(dashboard_rows(db))
        ]
        return {"section_key": section_key, "records": records}
    rows = record_repository.list_records(db, section_key)
    return {"section_key": section_key, "records": [serialize_record(row) for row in rows]}


def create_section_record(section_key: str, payload: dict, db: Session) -> dict:
    section = get_section(section_key)
    if not section.get("upload_enabled", True):
        raise HTTPException(400, f"{section['label']} is read-only and is refreshed from linked sections")
    record_data = payload.get("record_data") or payload
    custom_fields = payload.get("custom_fields", {}) if isinstance(payload, dict) else {}
    record_data, uploaded_derived = split_record_by_category(record_data, section)
    cleaned, status, errors = validate_record(record_data, section)
    row = record_repository.create_record(
        db,
        section_key=section_key,
        asset_id=cleaned.get("assetId"),
        record_data=cleaned,
        custom_fields=custom_fields,
        derived_audit={"uploaded_values": uploaded_derived, "fields": {}},
        validation_status=status,
        validation_errors=errors,
    )
    if row.asset_id:
        ensure_asset_section_shell_records(db, {row.asset_id})
        refresh_derived_records(db, {row.asset_id})
        refresh_portfolio_flat_records(db, {row.asset_id})
        row = record_repository.get_record(db, section_key, row.id) or row
    return serialize_record(row)


def update_section_record(section_key: str, record_id: int, payload: dict, db: Session) -> dict:
    section = get_section(section_key)
    if not section.get("upload_enabled", True):
        raise HTTPException(400, f"{section['label']} is read-only and is refreshed from linked sections")
    row = record_repository.get_record(db, section_key, record_id)
    if not row:
        raise HTTPException(404, "Record not found")
    previous_asset_id = row.asset_id
    record_data = payload.get("record_data", row.record_data or {})
    record_data, uploaded_derived = split_record_by_category(record_data, section)
    cleaned, status, errors = validate_record(record_data, section)
    updated = record_repository.update_record(
        db,
        row,
        asset_id=cleaned.get("assetId") or row.asset_id,
        record_data=cleaned,
        custom_fields=payload.get("custom_fields", row.custom_fields or {}),
        derived_audit={"uploaded_values": uploaded_derived, "fields": {}},
        validation_status=status,
        validation_errors=errors,
    )
    refresh_asset_ids = {asset_id for asset_id in [previous_asset_id, updated.asset_id] if asset_id}
    if refresh_asset_ids:
        ensure_asset_section_shell_records(db, {updated.asset_id} if updated.asset_id else set())
        refresh_derived_records(db, refresh_asset_ids)
        refresh_portfolio_flat_records(db, refresh_asset_ids)
        updated = record_repository.get_record(db, section_key, updated.id) or updated
    return serialize_record(updated)


def delete_section_record(section_key: str, record_id: int, db: Session) -> dict:
    section = get_section(section_key)
    if not section.get("upload_enabled", True):
        raise HTTPException(400, f"{section['label']} is read-only and is refreshed from linked sections")
    row = record_repository.get_record(db, section_key, record_id)
    if not row:
        raise HTTPException(404, "Record not found")
    asset_id = row.asset_id
    record_repository.delete_record(db, row)
    if asset_id:
        refresh_derived_records(db, {asset_id})
        refresh_portfolio_flat_records(db, {asset_id})
    return {"status": "deleted", "id": record_id}
