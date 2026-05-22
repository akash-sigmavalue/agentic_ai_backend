import math
import csv
import re
from pathlib import Path
from uuid import uuid4

import pandas as pd

from core.config import settings

SYSTEM_COLUMNS = {"source_sheet_name", "source_row_number", "source_table_index", "source_header_row_number"}


def normalize_text(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def save_upload(file) -> dict:
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload").suffix.lower()
    stored_name = f"{Path(file.filename or 'upload').stem}_{uuid4().hex}{suffix}"
    path = Path(settings.UPLOAD_DIR) / stored_name
    with path.open("wb") as handle:
        handle.write(file.file.read())
    return {"original_file_name": file.filename or stored_name, "file_path": str(path)}


def row_has_text(row) -> bool:
    values = [v for v in row.tolist() if not pd.isna(v) and str(v).strip()]
    if len(values) < 2:
        return False
    text_like = sum(1 for v in values if isinstance(v, str) or not str(v).replace('.', '', 1).isdigit())
    return text_like / max(len(values), 1) >= 0.45


def dedupe_headers(headers):
    seen = {}
    out = []
    for idx, h in enumerate(headers):
        name = str(h).strip() if not pd.isna(h) and str(h).strip() else f"Column {idx + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def dataframe_to_rows(df: pd.DataFrame) -> list[dict]:
    return [{str(k): clean_value(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def value_shape(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "empty"
    if re.fullmatch(r"[-+]?\d+(\.\d+)?%?", text):
        return "numeric_or_percentage"
    if re.search(r"[$₹€£]|rs\.?|inr|usd|cr|crore|lakh|mn|million|billion", text, re.I):
        return "currency_text"
    if re.search(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", text):
        return "date_text"
    if re.fullmatch(r"[A-Za-z]{1,6}[-_/]?\d{2,}", text):
        return "code_or_id"
    if re.search(r"\.(pdf|docx?|xlsx?|jpg|png)$", text, re.I):
        return "file_reference"
    if len(text) > 80:
        return "long_text"
    return "short_text"


def common_value_shapes(values) -> list[str]:
    counts = {}
    for value in values:
        shape = value_shape(value)
        counts[shape] = counts.get(shape, 0) + 1
    return [shape for shape, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:3]]


def _looks_like_date_column(normalized_col: str, sample_values: list[str], numeric_values) -> bool:
    if any(token in normalized_col for token in ["date", "timestamp", "expiry", "start", "end"]):
        return True
    if any(value_shape(value) == "date_text" for value in sample_values):
        return True
    if numeric_values.notna().any():
        numeric_min = numeric_values.dropna().min()
        return bool(numeric_min > 25000)
    return False


def row_blocks(raw: pd.DataFrame):
    non_empty = ~raw.isna().all(axis=1)
    blocks = []
    start = None
    for idx, present in enumerate(non_empty.tolist()):
        if present and start is None:
            start = idx
        elif not present and start is not None:
            blocks.append((start, idx))
            start = None
    if start is not None:
        blocks.append((start, len(raw)))
    return blocks


def detect_tables_in_excel(path: str) -> list[dict]:
    raw_sheets = pd.read_excel(path, sheet_name=None, header=None)
    detected = []
    table_index = 1
    for sheet_name, raw in raw_sheets.items():
        raw = raw.dropna(axis=1, how="all") if raw is not None else pd.DataFrame()
        if raw.empty:
            continue
        for start, end in row_blocks(raw):
            block = raw.iloc[start:end].dropna(axis=1, how="all")
            for header_pos in range(min(len(block), 10)):
                header_row = block.iloc[header_pos]
                if not row_has_text(header_row):
                    continue
                data = block.iloc[header_pos + 1:].dropna(how="all").copy()
                if data.empty:
                    continue
                data.columns = dedupe_headers(header_row.tolist())
                data = data.dropna(axis=1, how="all")
                headers = [str(c) for c in data.columns]
                if len(headers) < 2:
                    continue
                data["source_sheet_name"] = sheet_name
                data["source_row_number"] = range(start + header_pos + 2, start + header_pos + 2 + len(data))
                detected.append({
                    "table_index": table_index,
                    "sheet_name": sheet_name,
                    "header_row_number": start + header_pos + 1,
                    "detected_section_key": None,
                    "section_confidence": 0,
                    "section_candidates": [],
                    "dataframe": data,
                })
                table_index += 1
                break
    return detected


def read_csv_raw(path: str) -> pd.DataFrame:
    try:
        raw = pd.read_csv(path, header=None, sep=None, engine="python", skip_blank_lines=False)
    except (csv.Error, pd.errors.ParserError):
        with open(path, newline="", encoding="utf-8-sig") as handle:
            lines = handle.readlines()
        sample = "".join(line for line in lines if line.strip())
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        raw = pd.DataFrame(list(csv.reader(lines, dialect)))
    return raw.replace(r"^\s*$", pd.NA, regex=True)


def detect_tables_in_csv(path: str) -> list[dict]:
    raw = read_csv_raw(path)
    raw = raw.dropna(axis=1, how="all") if raw is not None else pd.DataFrame()
    if raw.empty:
        return []

    detected = []
    table_index = 1
    for start, end in row_blocks(raw):
        block = raw.iloc[start:end].dropna(axis=1, how="all")
        for header_pos in range(min(len(block), 10)):
            header_row = block.iloc[header_pos]
            if not row_has_text(header_row):
                continue
            data = block.iloc[header_pos + 1:].dropna(how="all").copy()
            if data.empty:
                continue
            data.columns = dedupe_headers(header_row.tolist())
            data = data.dropna(axis=1, how="all")
            headers = [str(c) for c in data.columns]
            if len(headers) < 2:
                continue
            header_row_number = start + header_pos + 1
            data["source_sheet_name"] = "csv"
            data["source_row_number"] = range(header_row_number + 1, header_row_number + 1 + len(data))
            data["source_table_index"] = table_index
            data["source_header_row_number"] = header_row_number
            detected.append({
                "table_index": table_index,
                "sheet_name": "csv",
                "header_row_number": header_row_number,
                "detected_section_key": None,
                "section_confidence": 0,
                "section_candidates": [],
                "dataframe": data,
            })
            table_index += 1
            break
    return detected


def detect_tables(path: str) -> list[dict]:
    suffix = Path(path).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return detect_tables_in_excel(path)
    if suffix == ".csv":
        return detect_tables_in_csv(path)
    raise ValueError("Only .xlsx, .xls, and .csv files are supported")


def profile_columns(df: pd.DataFrame) -> list[dict]:
    profiles = []
    user_columns = [c for c in df.columns if c not in SYSTEM_COLUMNS]
    total = max(len(df), 1)
    for col in user_columns:
        series = df[col]
        non_null = series.dropna()
        numeric = pd.to_numeric(non_null, errors="coerce")
        normalized_col = normalize_text(col)
        sample_values = [str(v) for v in non_null.astype(str).drop_duplicates().head(12).tolist()]
        numeric_ratio = 0 if non_null.empty else float(numeric.notna().mean())
        looks_like_date = _looks_like_date_column(normalized_col, sample_values, numeric)
        dates = pd.to_datetime(non_null, errors="coerce") if looks_like_date else None
        date_ratio = 0 if non_null.empty or dates is None else float(dates.notna().mean())
        profiles.append({
            "column_name": str(col),
            "normalized_name": normalized_col,
            "sample_values": sample_values,
            "null_percentage": round(float(series.isna().sum() / total * 100), 2),
            "unique_count": int(non_null.nunique()),
            "numeric_ratio": round(numeric_ratio, 3),
            "date_ratio": round(date_ratio, 3),
            "detected_type": "number" if not non_null.empty and numeric_ratio >= 0.8 else "date" if not non_null.empty and date_ratio >= 0.7 else "text",
            "value_shapes": common_value_shapes(sample_values),
            "looks_like_amount": any(t in normalized_col for t in ["amount", "value", "cost", "rent", "emi", "price", "tax", "insurance", "capex", "opex", "funded", "principal", "outstanding"]),
            "looks_like_date": looks_like_date,
            "looks_like_id": "id" in normalized_col or "code" in normalized_col or any(value_shape(v) == "code_or_id" for v in sample_values),
            "looks_like_percentage": "%" in normalized_col or "rate" in normalized_col or "ratio" in normalized_col or any(str(v).strip().endswith("%") for v in sample_values),
            "looks_like_file": any(value_shape(v) == "file_reference" for v in sample_values),
        })
    return profiles
