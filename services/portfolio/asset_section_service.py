from sqlalchemy.orm import Session

from database.portfolio.repositories.record_repository import create_record, get_record_by_asset
from registry.portfolio.registry import upload_sections
from tools.portfolio.validation import validate_record


def ensure_asset_section_shell_records(db: Session, asset_ids: set[str] | list[str]) -> int:
    created_count = 0
    for asset_id in sorted({str(item).strip() for item in asset_ids if str(item or "").strip()}):
        for section in upload_sections():
            section_key = section["section_key"]
            if get_record_by_asset(db, section_key, asset_id):
                continue
            id_field = section.get("id_field", "assetId")
            record_data = {id_field: asset_id}
            cleaned, status, errors = validate_record(record_data, section)
            create_record(
                db,
                section_key=section_key,
                asset_id=asset_id,
                record_data=cleaned,
                custom_fields={},
                derived_audit={"uploaded_values": {}, "fields": {}},
                validation_status=status,
                validation_errors=errors,
            )
            created_count += 1
    return created_count
