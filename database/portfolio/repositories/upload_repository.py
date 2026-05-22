from sqlalchemy.orm import Session

from database.portfolio.models import Upload, UploadTable


def create_upload(db: Session, *, mode: str, section_key: str | None, saved_file: dict, table_count: int) -> Upload:
    upload = Upload(
        mode=mode,
        section_key=section_key,
        original_file_name=saved_file["original_file_name"],
        file_path=saved_file["file_path"],
        table_count=table_count,
        status="previewed",
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return upload


def get_upload(db: Session, upload_id: int) -> Upload | None:
    return db.query(Upload).filter(Upload.id == upload_id).one_or_none()


def save_upload_table(db: Session, *, upload_id: int, table: dict, section_key: str | None, section_confidence: float, columns_profile: list, preview_rows: list, source_rows: list) -> UploadTable:
    row = UploadTable(
        upload_id=upload_id,
        table_index=table["table_index"],
        sheet_name=table.get("sheet_name"),
        header_row_number=table.get("header_row_number"),
        detected_section_key=section_key,
        section_confidence=float(section_confidence or 0),
        columns_profile=columns_profile,
        preview_rows=preview_rows,
        source_rows=source_rows,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_upload_tables(db: Session, upload_id: int) -> list[UploadTable]:
    return db.query(UploadTable).filter(UploadTable.upload_id == upload_id).order_by(UploadTable.table_index.asc()).all()


def mark_upload_confirmed(db: Session, upload: Upload) -> Upload:
    upload.status = "confirmed"
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return upload
