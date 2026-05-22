from sqlalchemy.orm import Session

from database.portfolio.models import Record


def list_records(db: Session, section_key: str | None = None) -> list[Record]:
    query = db.query(Record)
    if section_key:
        query = query.filter(Record.section_key == section_key)
    return query.order_by(Record.id.desc()).all()


def list_asset_ids(db: Session) -> list[str]:
    rows = db.query(Record.asset_id).filter(Record.asset_id.isnot(None)).all()
    return [asset_id for (asset_id,) in rows if asset_id]


def get_record(db: Session, section_key: str, record_id: int) -> Record | None:
    return db.query(Record).filter(Record.id == record_id, Record.section_key == section_key).one_or_none()


def get_record_by_asset(db: Session, section_key: str, asset_id: str) -> Record | None:
    return db.query(Record).filter(Record.section_key == section_key, Record.asset_id == asset_id).order_by(Record.id.asc()).first()


def create_record(db: Session, **kwargs) -> Record:
    row = Record(**kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_record(db: Session, row: Record, **kwargs) -> Record:
    for key, value in kwargs.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_record(db: Session, row: Record) -> None:
    db.delete(row)
    db.commit()
