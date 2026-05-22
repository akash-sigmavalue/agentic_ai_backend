from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from database.portfolio.models import PortfolioFlatRecord, Record
from services.portfolio.portfolio_flat_mapping import PORTFOLIO_FLAT_FIELD_MAPPING, PORTFOLIO_FLAT_NUMERIC_COLUMNS
from tools.portfolio.validation import parse_number


def get_records_for_flat_refresh(db: Session, asset_ids: set[str] | list[str] | None = None) -> list[Record]:
    query = db.query(Record).filter(Record.asset_id.isnot(None))
    if asset_ids is not None:
        normalized_asset_ids = _normalize_asset_ids(asset_ids)
        if not normalized_asset_ids:
            return []
        query = query.filter(Record.asset_id.in_(normalized_asset_ids))
    return query.order_by(Record.asset_id.asc(), Record.id.asc()).all()


def refresh_portfolio_flat_records(db: Session, asset_ids: set[str] | list[str] | None = None) -> None:
    records = get_records_for_flat_refresh(db, asset_ids)
    records_by_asset: dict[str, list[Record]] = defaultdict(list)
    for row in records:
        asset_id = str(row.asset_id or "").strip()
        if asset_id:
            records_by_asset[asset_id].append(row)

    if asset_ids is None:
        db.query(PortfolioFlatRecord).delete()
        for asset_id, asset_records in records_by_asset.items():
            db.add(PortfolioFlatRecord(**_flat_payload(asset_id, asset_records)))
        db.commit()
        return

    requested_asset_ids = _normalize_asset_ids(asset_ids)
    for asset_id in requested_asset_ids:
        flat_row = db.query(PortfolioFlatRecord).filter(PortfolioFlatRecord.asset_id == asset_id).one_or_none()
        asset_records = records_by_asset.get(asset_id, [])
        if not asset_records:
            if flat_row:
                db.delete(flat_row)
            continue

        payload = _flat_payload(asset_id, asset_records)
        if flat_row:
            for key, value in payload.items():
                if key != "asset_id":
                    setattr(flat_row, key, value)
            db.add(flat_row)
        else:
            db.add(PortfolioFlatRecord(**payload))
    db.commit()


def _flat_payload(asset_id: str, records: list[Record]) -> dict[str, Any]:
    payload: dict[str, Any] = {"asset_id": asset_id}
    for row in records:
        section_key = row.section_key
        record_data = row.record_data or {}
        for source_field, value in record_data.items():
            if source_field == "assetId":
                continue
            flat_column = PORTFOLIO_FLAT_FIELD_MAPPING.get((section_key, source_field))
            if not flat_column:
                continue
            payload[flat_column] = _clean_flat_value(flat_column, value)
    return payload


def _clean_flat_value(flat_column: str, value: Any) -> Any:
    if value in (None, ""):
        return None
    if flat_column in PORTFOLIO_FLAT_NUMERIC_COLUMNS:
        return parse_number(value)
    return str(value)


def _normalize_asset_ids(asset_ids: set[str] | list[str]) -> list[str]:
    return sorted({str(asset_id).strip() for asset_id in asset_ids if str(asset_id or "").strip()})
