from sqlalchemy.orm import Session

from database.portfolio.models import ColumnMapping


def save_mapping_rows(db: Session, *, upload_id: int, table_index: int, section_key: str, items: list[dict]) -> None:
    for item in items:
        db.add(
            ColumnMapping(
                upload_id=upload_id,
                table_index=table_index,
                section_key=section_key,
                uploaded_column=item.get("uploaded_column", ""),
                target_field=item.get("target_field"),
                confidence=float(item.get("confidence") or 0),
                status=item.get("status", "custom_field"),
                reason=item.get("reason", ""),
            )
        )
    db.commit()


def replace_upload_mapping(db: Session, *, upload_id: int, tables: list[dict]) -> None:
    db.query(ColumnMapping).filter(ColumnMapping.upload_id == upload_id).delete()
    db.commit()
    for table in tables:
        items = [*table.get("mappings", []), *table.get("unmapped_columns", [])]
        save_mapping_rows(
            db,
            upload_id=upload_id,
            table_index=int(table["table_index"]),
            section_key=table["section_key"],
            items=items,
        )


def list_mapping_rows(db: Session, upload_id: int) -> list[ColumnMapping]:
    return db.query(ColumnMapping).filter(ColumnMapping.upload_id == upload_id).order_by(ColumnMapping.table_index.asc(), ColumnMapping.id.asc()).all()


def list_table_mapping_rows(db: Session, upload_id: int, table_index: int) -> list[ColumnMapping]:
    return db.query(ColumnMapping).filter(ColumnMapping.upload_id == upload_id, ColumnMapping.table_index == table_index).all()


def replace_upload_mapping_rows(db: Session, *, upload_id: int, tables: list[dict]) -> None:
    db.query(ColumnMapping).filter(ColumnMapping.upload_id == upload_id).delete()
    for table in tables:
        items = [*table.get("mappings", []), *table.get("unmapped_columns", [])]
        for item in items:
            db.add(
                ColumnMapping(
                    upload_id=upload_id,
                    table_index=int(table["table_index"]),
                    section_key=table["section_key"],
                    uploaded_column=item.get("uploaded_column", ""),
                    target_field=item.get("target_field"),
                    confidence=float(item.get("confidence") or 0),
                    status=item.get("status", "custom_field"),
                    reason=item.get("reason", ""),
                )
            )
    db.commit()
