from fastapi import HTTPException
from sqlalchemy.orm import Session
import json
import re

from agents.mapping_agent.main import MappingAgent
from core.config import settings
from database.portfolio.repositories import mapping_repository, upload_repository
from database.portfolio.repositories.record_repository import create_record, list_asset_ids
from registry.portfolio.registry import get_section, upload_sections
from services.portfolio.asset_section_service import ensure_asset_section_shell_records
from services.portfolio.dashboard_service import dashboard
from services.portfolio.derived_calculation_service import refresh_derived_records
from services.portfolio.portfolio_flat_service import refresh_portfolio_flat_records
from tools.portfolio.upload_tools import SYSTEM_COLUMNS, clean_value, dataframe_to_rows, detect_tables, normalize_text, profile_columns, save_upload
from tools.portfolio.validation import validate_record


GENERATED_ASSET_ID_PREFIX = "PMS"
GENERATED_ASSET_ID_PATTERN = re.compile(r"^PMS-(\d+)$", re.IGNORECASE)


ASSET_ID_COLUMN_NAMES = {
    "asset id",
    "assetid",
    "asset code",
    "assetcode",
    "property id",
    "propertyid",
    "property code",
    "propertycode",
    "building id",
    "buildingid",
}


class AssetIdGenerator:
    def __init__(self, existing_asset_ids: list[str]):
        self.used_numbers = {
            int(match.group(1))
            for asset_id in existing_asset_ids
            if (match := GENERATED_ASSET_ID_PATTERN.match(str(asset_id).strip()))
        }
        self.next_number = max(self.used_numbers, default=0) + 1

    def next(self) -> str:
        while self.next_number in self.used_numbers:
            self.next_number += 1
        value = f"{GENERATED_ASSET_ID_PREFIX}-{self.next_number:03d}"
        self.used_numbers.add(self.next_number)
        self.next_number += 1
        return value


def _mapping_items(mapping: dict) -> list[dict]:
    return [*mapping.get("mappings", []), *mapping.get("unmapped_columns", [])]


def _section_mappings(mapping: dict) -> list[dict]:
    if mapping.get("section_mappings"):
        return mapping["section_mappings"]
    if mapping.get("section_key"):
        return [mapping]
    return []


def _section_id_field(section_key: str) -> str:
    try:
        return get_section(section_key).get("id_field", "assetId")
    except Exception:
        return "assetId"


def _require_upload_enabled(section_key: str) -> dict:
    section = get_section(section_key)
    if not section.get("upload_enabled", True):
        raise HTTPException(400, f"{section['label']} is read-only and cannot be used for upload mapping")
    return section


def _business_mapping_count(section_mapping: dict) -> int:
    id_field = _section_id_field(section_mapping.get("section_key"))
    return sum(1 for item in section_mapping.get("mappings", []) if item.get("target_field") and item.get("target_field") != id_field)


def _has_business_mapping(section_mapping: dict) -> bool:
    return _business_mapping_count(section_mapping) > 0


def _preview_payload(table: dict, profile: list[dict], mapping: dict, preview_rows: list[dict], section_key: str | None, confidence: float | None = None) -> dict:
    return {
        "table_index": table["table_index"],
        "sheet_name": table.get("sheet_name"),
        "header_row_number": table.get("header_row_number"),
        "detected_section_key": section_key,
        "section_confidence": confidence if confidence is not None else table.get("section_confidence"),
        "section_candidates": table.get("section_candidates", []),
        "uploaded_columns": [p["column_name"] for p in profile],
        "column_profile": profile,
        "mapping_result": mapping,
        "preview_rows": preview_rows,
    }


def _looks_like_asset_id_value(value) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(token in normalized for token in ["asset", "property", "building"]) and any(char.isdigit() for char in text)


def _asset_id_from_row(row: dict, mapped_by_column: dict, id_field: str) -> str | None:
    for column, target in mapped_by_column.items():
        if target == id_field and row.get(column) not in (None, ""):
            return clean_value(row.get(column))

    for column, value in row.items():
        if column in SYSTEM_COLUMNS or value in (None, ""):
            continue
        normalized_column = normalize_text(column)
        compact_column = normalized_column.replace(" ", "")
        if normalized_column in ASSET_ID_COLUMN_NAMES or compact_column in ASSET_ID_COLUMN_NAMES:
            return clean_value(value)

    for column, value in row.items():
        if column in SYSTEM_COLUMNS or value in (None, ""):
            continue
        if _looks_like_asset_id_value(value):
            return clean_value(value)
    return None


def _generated_asset_cache_key(upload_mode: str, table_index: int | None, source_row_number: int) -> tuple:
    if upload_mode == "global":
        return ("global", source_row_number)
    return ("section", table_index, source_row_number)


def _resolve_asset_id(
    *,
    row: dict,
    mapped_by_column: dict,
    id_field: str,
    upload_mode: str,
    table_index: int | None,
    source_row_number: int,
    generator: AssetIdGenerator,
    generated_asset_ids: dict[tuple, str],
) -> str:
    detected_asset_id = _asset_id_from_row(row, mapped_by_column, id_field)
    if detected_asset_id:
        return detected_asset_id

    cache_key = _generated_asset_cache_key(upload_mode, table_index, source_row_number)
    if cache_key not in generated_asset_ids:
        generated_asset_ids[cache_key] = generator.next()
    return generated_asset_ids[cache_key]


def _field_categories(section: dict) -> dict[str, str]:
    return {field["key"]: field.get("category", "raw") for field in section.get("fields", [])}


def _empty_mapping(reason: str = "No section could be confidently selected") -> dict:
    return {
        "section_key": None,
        "section_confidence": 0,
        "mappings": [],
        "unmapped_columns": [],
        "reasoning": reason,
    }


def _print_mapping_agent_arguments(label: str, payload: dict) -> None:
    if not settings.DEBUG_MAPPING_AGENT:
        return
    print(
        f"\nUPLOAD_SERVICE_{label}\n{json.dumps(payload, indent=2, default=str)}",
        flush=True,
    )


def _save_table_mapping(db: Session, *, upload_id: int, table: dict, section_key: str | None, profile: list[dict], mapping: dict, source_rows: list[dict], preview_rows: list[dict], confidence: float) -> None:
    upload_repository.save_upload_table(
        db,
        upload_id=upload_id,
        table=table,
        section_key=section_key,
        section_confidence=confidence,
        columns_profile=profile,
        preview_rows=preview_rows,
        source_rows=source_rows,
    )
    for section_mapping in _section_mappings(mapping):
        if not _has_business_mapping(section_mapping):
            continue
        mapping_repository.save_mapping_rows(
            db,
            upload_id=upload_id,
            table_index=table["table_index"],
            section_key=section_mapping["section_key"],
            items=_mapping_items(section_mapping),
        )


def save_section_table_and_mapping(db: Session, *, upload_id: int, table: dict, section_key: str) -> dict:
    section = _require_upload_enabled(section_key)
    df = table["dataframe"]
    profile = profile_columns(df)
    source_rows = dataframe_to_rows(df)
    preview_rows = source_rows[: settings.MAX_PREVIEW_ROWS]

    # Section upload: agent only receives this section's fields.
    table_context = {
        "sheet_name": table.get("sheet_name"),
        "header_row_number": table.get("header_row_number"),
        "table_index": table.get("table_index"),
        "uploaded_column_count": len(profile),
        "uploaded_columns": [item["column_name"] for item in profile],
    }
    _print_mapping_agent_arguments(
        "MAP_SECTION_COLUMNS_INPUT_ARGS",
        {
            "method": "MappingAgent.map_section_columns",
            "arguments": {
                "section": section,
                "column_profile": profile,
                "sample_rows": preview_rows,
                "table_context": table_context,
            },
        },
    )
    mapping = MappingAgent().map_section_columns(section, profile, preview_rows, table_context)
    _print_mapping_agent_arguments(
        "MAP_SECTION_COLUMNS_OUTPUT",
        {
            "method": "MappingAgent.map_section_columns",
            "return_value": mapping,
        },
    )
    confidence = float(mapping.get("section_confidence") or 0)
    _save_table_mapping(
        db,
        upload_id=upload_id,
        table=table,
        section_key=section_key,
        profile=profile,
        mapping=mapping,
        source_rows=source_rows,
        preview_rows=preview_rows,
        confidence=confidence,
    )
    return _preview_payload(table, profile, mapping, preview_rows, section_key, confidence)


def save_global_table_and_mapping(db: Session, *, upload_id: int, table: dict) -> list[dict]:
    df = table["dataframe"]
    profile = profile_columns(df)
    source_rows = dataframe_to_rows(df)
    preview_rows = source_rows[: settings.MAX_PREVIEW_ROWS]

    # Global upload: agent receives the frontend registry for all upload-enabled sections,
    # selects one section, then maps only into that chosen section.
    table_context = {
        "sheet_name": table.get("sheet_name"),
        "header_row_number": table.get("header_row_number"),
        "table_index": table.get("table_index"),
        "uploaded_column_count": len(profile),
        "uploaded_columns": [item["column_name"] for item in profile],
    }
    mapping = MappingAgent().map_global_table(upload_sections(), profile, preview_rows, table_context)
    section_key = mapping.get("section_key")
    confidence = float(mapping.get("section_confidence") or 0)
    _save_table_mapping(
        db,
        upload_id=upload_id,
        table=table,
        section_key=section_key,
        profile=profile,
        mapping=mapping,
        source_rows=source_rows,
        preview_rows=preview_rows,
        confidence=confidence,
    )
    section_mappings = [section_mapping for section_mapping in _section_mappings(mapping) if _has_business_mapping(section_mapping)]
    if not section_mappings:
        return [_preview_payload(table, profile, mapping, preview_rows, section_key, confidence)]
    return [
        _preview_payload(
            table,
            profile,
            section_mapping,
            preview_rows,
            section_mapping.get("section_key"),
            float(section_mapping.get("section_confidence") or 0),
        )
        for section_mapping in section_mappings
    ]


def _preview_score(preview: dict) -> tuple[int, float, int]:
    mapping = preview.get("mapping_result") or {}
    return (
        _business_mapping_count(mapping),
        float(preview.get("section_confidence") or 0),
        len(mapping.get("mappings", [])),
    )


def _dedupe_global_previews(db: Session, upload_id: int, previews: list[dict]) -> list[dict]:
    best_by_section = {}
    for preview in previews:
        section_key = preview.get("detected_section_key")
        if not section_key:
            continue
        current = best_by_section.get(section_key)
        if current is None or _preview_score(preview) > _preview_score(current):
            best_by_section[section_key] = preview

    selected = sorted(best_by_section.values(), key=lambda item: (item.get("table_index") or 0, item.get("detected_section_key") or ""))
    mapping_repository.replace_upload_mapping_rows(
        db,
        upload_id=upload_id,
        tables=[
            {
                "table_index": preview["table_index"],
                "section_key": preview["detected_section_key"],
                "mappings": (preview.get("mapping_result") or {}).get("mappings", []),
                "unmapped_columns": (preview.get("mapping_result") or {}).get("unmapped_columns", []),
            }
            for preview in selected
        ],
    )
    return selected


def preview_section_upload(section_key: str, file, db: Session) -> dict:
    _require_upload_enabled(section_key)
    saved = save_upload(file)
    tables = detect_tables(saved["file_path"])
    if not tables:
        raise HTTPException(400, "No usable tables found in upload")
    upload = upload_repository.create_upload(db, mode="section", section_key=section_key, saved_file=saved, table_count=len(tables))
    previews = [save_section_table_and_mapping(db, upload_id=upload.id, table=table, section_key=section_key) for table in tables]
    return {"upload_id": upload.id, "mode": "section", "section_key": section_key, "file_name": saved["original_file_name"], "tables": previews}


def preview_global_upload(file, db: Session) -> dict:
    saved = save_upload(file)
    tables = detect_tables(saved["file_path"])
    if not tables:
        raise HTTPException(400, "No usable tables found in upload")
    upload = upload_repository.create_upload(db, mode="global", section_key=None, saved_file=saved, table_count=len(tables))
    previews = []
    for table in tables:
        previews.extend(save_global_table_and_mapping(db, upload_id=upload.id, table=table))
    previews = _dedupe_global_previews(db, upload.id, previews)
    return {"upload_id": upload.id, "mode": "global", "file_name": saved["original_file_name"], "tables": previews}


def mapping_payload(upload_id: int, db: Session) -> dict:
    rows = mapping_repository.list_mapping_rows(db, upload_id)
    by_table = {}
    for row in rows:
        item = by_table.setdefault((row.table_index, row.section_key), {"table_index": row.table_index, "section_key": row.section_key, "mappings": [], "unmapped_columns": []})
        payload = {
            "uploaded_column": row.uploaded_column,
            "target_field": row.target_field,
            "confidence": row.confidence,
            "status": row.status,
            "reason": row.reason,
        }
        if row.target_field:
            item["mappings"].append(payload)
        else:
            item["unmapped_columns"].append(payload)
    return {"upload_id": upload_id, "tables": list(by_table.values())}


def patch_upload_mapping(upload_id: int, payload: dict, db: Session) -> dict:
    valid_tables = []
    for table in payload.get("tables", []):
        section_key = table.get("section_key")
        if not section_key:
            continue
        section = _require_upload_enabled(section_key)
        allowed = {f["key"] for f in section["fields"]}
        for item in [*table.get("mappings", []), *table.get("unmapped_columns", [])]:
            target = item.get("target_field")
            if target and target not in allowed:
                raise HTTPException(400, f"{target} is not allowed for {section_key}")
            item["status"] = "confirmed" if target and item.get("status") in {None, "", "custom_field"} else item.get("status", "custom_field")
            item["reason"] = item.get("reason") or "User mapping"
        valid_tables.append(table)
    mapping_repository.replace_upload_mapping(db, upload_id=upload_id, tables=valid_tables)
    return mapping_payload(upload_id, db)


def confirm_upload(upload_id: int, db: Session) -> dict:
    upload = upload_repository.get_upload(db, upload_id)
    if not upload:
        raise HTTPException(404, "Upload not found")

    saved_count = 0
    asset_id_generator = AssetIdGenerator(list_asset_ids(db))
    generated_asset_ids: dict[tuple, str] = {}
    imported_asset_ids: set[str] = set()
    for table in upload_repository.list_upload_tables(db, upload_id):
        mappings = mapping_repository.list_table_mapping_rows(db, upload_id, table.table_index)
        mappings_by_section: dict[str, list] = {}
        for mapping in mappings:
            mappings_by_section.setdefault(mapping.section_key, []).append(mapping)
        for section_key, section_mappings in mappings_by_section.items():
            section = _require_upload_enabled(section_key)
            mapped_by_column = {
                m.uploaded_column: m.target_field
                for m in section_mappings
                if m.target_field and m.status in {"auto_mapped", "needs_review", "confirmed"}
            }
            if not mapped_by_column:
                continue
            categories = _field_categories(section)
            for fallback_row_number, row in enumerate(table.source_rows or [], start=1):
                record_data = {}
                custom_fields = {}
                uploaded_derived = {}
                source_row_number = row.get("source_row_number") or fallback_row_number
                for col, value in row.items():
                    if col in SYSTEM_COLUMNS:
                        continue
                    target = mapped_by_column.get(col)
                    if target:
                        cleaned_value = clean_value(value)
                        if categories.get(target) == "derived":
                            if cleaned_value not in (None, ""):
                                uploaded_derived[target] = cleaned_value
                        else:
                            record_data[target] = cleaned_value
                    elif upload.mode == "section":
                        custom_fields[col] = clean_value(value)
                id_field = section.get("id_field", "assetId")
                asset_id = record_data.get(id_field) or _resolve_asset_id(
                    row=row,
                    mapped_by_column=mapped_by_column,
                    id_field=id_field,
                    upload_mode=upload.mode,
                    table_index=table.table_index,
                    source_row_number=source_row_number,
                    generator=asset_id_generator,
                    generated_asset_ids=generated_asset_ids,
                )
                record_data[id_field] = asset_id
                cleaned, status, errors = validate_record(record_data, section)
                if not cleaned and not custom_fields:
                    continue
                asset_id = cleaned.get(id_field) or asset_id or custom_fields.get("Asset ID") or custom_fields.get("Asset Id")
                if asset_id:
                    imported_asset_ids.add(str(asset_id))
                create_record(
                    db,
                    section_key=section["section_key"],
                    asset_id=asset_id,
                    record_data=cleaned,
                    custom_fields=custom_fields,
                    derived_audit={"uploaded_values": uploaded_derived, "fields": {}},
                    validation_status=status,
                    validation_errors=errors,
                    source_upload_id=upload_id,
                    source_table_index=table.table_index,
                    source_sheet_name=table.sheet_name,
                    source_row_number=source_row_number,
                )
                saved_count += 1
    if imported_asset_ids:
        shell_count = ensure_asset_section_shell_records(db, imported_asset_ids) if upload.mode == "global" else 0
        refresh_derived_records(db, imported_asset_ids)
        refresh_portfolio_flat_records(db, imported_asset_ids)
    else:
        shell_count = 0
    upload_repository.mark_upload_confirmed(db, upload)
    return {"upload_id": upload_id, "status": "confirmed", "saved_count": saved_count, "shell_record_count": shell_count, "dashboard": dashboard(db)}
