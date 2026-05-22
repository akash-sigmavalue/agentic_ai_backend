from datetime import datetime, timedelta

import pandas as pd

from tools.portfolio.upload_tools import clean_value


def parse_number(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("₹", "").replace("%", "").strip())
    except Exception:
        return None


def parse_date(value):
    if value in (None, ""):
        return None
    if hasattr(value, "date") and callable(value.date):
        return value.date().isoformat()
    if isinstance(value, (int, float)) and value > 25000:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = pd.to_datetime(text, errors="coerce")
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def validate_record(record_data, section):
    errors = []
    cleaned = dict(record_data)
    for field in section["fields"]:
        key = field["key"]
        value = cleaned.get(key)
        if field.get("required") and value in (None, ""):
            errors.append({"field": key, "severity": "invalid", "message": "Required field is missing."})
            continue
        if value in (None, ""):
            continue
        if field["data_type"] in {"number", "currency", "percentage", "integer"}:
            parsed = parse_number(value)
            if parsed is None:
                errors.append({"field": key, "severity": "warning", "message": "Value is not numeric."})
            else:
                cleaned[key] = int(parsed) if field["data_type"] == "integer" else parsed
        elif field["data_type"] == "date":
            parsed = parse_date(value)
            if parsed is None:
                errors.append({"field": key, "severity": "warning", "message": "Value is not a date."})
            else:
                cleaned[key] = parsed
        else:
            cleaned[key] = clean_value(value)
    status = "invalid" if any(e["severity"] == "invalid" for e in errors) else "warning" if errors else "valid"
    return cleaned, status, errors
