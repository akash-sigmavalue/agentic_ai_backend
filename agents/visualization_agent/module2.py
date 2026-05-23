import ast
import json
import os
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


# ============================================================
# CONFIGURATION
# ============================================================

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-5.4-mini"

_DB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "database", "visualization_agent")
DEFAULT_DATA_MAPPING_PATH = os.path.join(_DB_DIR, "data_mapping.py")
DEFAULT_MODULE_1_INTENT_PATH = os.path.join(_DB_DIR, "module_1_intent.json")
DEFAULT_RETRIEVAL_CONTEXT_PATH = os.path.join(_DB_DIR, "retrival_model.json")
DEFAULT_RETRIEVED_DATA_PATH = os.path.join(_DB_DIR, "retrival_model_sample_input.xlsx")
DEFAULT_RETRIEVAL_SQL_PATH = os.path.join(_DB_DIR, "baner_balewadi_transaction_query.sql")

SOURCE_TYPE = "proprietary_database"
ROW_LIMIT_APPLIED = False

USE_LLM_FOR_COLUMN_MAPPING = True
USE_LLM_FOR_TRANSFORMATION_PLAN = True
USE_LLM_FOR_MISSING_EXPLANATION = True
USE_LLM_FOR_VISUALIZATION_STRUCTURE = True
USE_LLM_FOR_STEP_SUMMARY = True

MODEL_PRICING_USD_PER_1M_TOKENS = {
    "gpt-5.5": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
    "gpt-5.4-mini": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
}


# ============================================================
# TOKEN LEDGER (per-request, replaces st.session_state)
# ============================================================

class TokenLedger:
    """Per-request token usage tracker (replaces Streamlit session state)."""

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []
        self.total_cost_usd: float = 0.0
        self.call_counter: int = 0

    def add(self, call_name: str, model: str, usage_data: Dict[str, int], cost_data: Dict[str, float]) -> None:
        self.call_counter += 1
        row = {
            "call_id": self.call_counter,
            "timestamp": now_str(),
            "call_name": call_name,
            "model": model,
            "input_tokens": usage_data["input_tokens"],
            "cached_input_tokens": usage_data["cached_input_tokens"],
            "output_tokens": usage_data["output_tokens"],
            "total_tokens": usage_data["total_tokens"],
            "input_cost_usd": cost_data["input_cost"],
            "cached_input_cost_usd": cost_data["cached_input_cost"],
            "output_cost_usd": cost_data["output_cost"],
            "total_cost_usd": cost_data["total_cost"],
        }
        self.entries.append(row)
        self.total_cost_usd += cost_data["total_cost"]

    def summary(self) -> Dict[str, Any]:
        return {
            "total_llm_calls": len(self.entries),
            "total_input_tokens": sum(x["input_tokens"] for x in self.entries),
            "total_cached_input_tokens": sum(x["cached_input_tokens"] for x in self.entries),
            "total_output_tokens": sum(x["output_tokens"] for x in self.entries),
            "total_tokens": sum(x["total_tokens"] for x in self.entries),
            "total_cost_usd": round(self.total_cost_usd, 8),
            "ledger": self.entries,
        }


# Module-level ledger instance used during a single run_module_2 call.
_active_ledger: Optional[TokenLedger] = None


# ============================================================
# GENERAL UTILITY FUNCTIONS
# ============================================================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"[^\w\s./]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_column_name(col: Any) -> str:
    text = str(col).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def safe_json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def ensure_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def ensure_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in [None, "", [], {}]:
            return value
    return None


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def is_api_available() -> bool:
    return bool(OPENAI_API_KEY.strip()) and OpenAI is not None


def extract_json_from_text(text: str) -> Dict[str, Any]:
    if not text:
        raise ValueError("Empty LLM response.")

    cleaned = text.strip()
    fenced_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced_match:
        cleaned = fenced_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last == -1:
        raise ValueError("No JSON object found in response.")

    return json.loads(cleaned[first:last + 1])


def is_numeric_series(series: pd.Series) -> bool:
    non_null = series.dropna()
    if non_null.empty:
        return False
    converted = pd.to_numeric(non_null, errors="coerce")
    return converted.notna().mean() >= 0.80


def looks_like_location_column(column_name: str, meaning: str = "") -> bool:
    text = normalize_text(f"{column_name} {meaning}")
    positive_terms = [
        "location",
        "locality",
        "village",
        "micromarket",
        "micro market",
        "sub locality",
        "area",
        "region",
        "city",
        "geo",
    ]
    negative_terms = [
        "latitude",
        "longitude",
        "lat",
        "long",
        "sq",
        "area sq",
        "carpet",
        "price",
        "value",
        "count",
        "transaction",
        "metric",
        "rate",
    ]

    has_positive = any(term in text for term in positive_terms)
    has_negative = any(term in text for term in negative_terms)
    return has_positive and not has_negative


def choose_best_location_column(df: pd.DataFrame, column_mapping: Dict[str, str]) -> Optional[str]:
    priority_names = [
        "location_name",
        "village_name",
        "micro_market",
        "micromarket",
        "sub_locality",
        "city_name",
        "project_name",
    ]

    normalized_cols = {normalize_text(col): col for col in df.columns}

    for name in priority_names:
        if normalize_text(name) in normalized_cols:
            col = normalized_cols[normalize_text(name)]
            if not is_numeric_series(df[col]):
                return col

    candidates = []
    for col in df.columns:
        meaning = column_mapping.get(col, "")
        if looks_like_location_column(col, meaning) and not is_numeric_series(df[col]):
            candidates.append(col)

    if candidates:
        return candidates[0]

    text_like_cols = []
    for col in df.columns:
        if not is_numeric_series(df[col]):
            text_like_cols.append(col)

    for col in text_like_cols:
        if any(term in normalize_text(col) for term in ["name", "location", "village", "market"]):
            return col

    return None


def is_valid_geo_field(df: pd.DataFrame, col: Optional[str], column_mapping: Dict[str, str]) -> bool:
    if not col or col not in df.columns:
        return False

    if is_numeric_series(df[col]):
        return False

    meaning = column_mapping.get(col, "")
    text = normalize_text(f"{col} {meaning}")

    bad_terms = [
        "carpet",
        "area sq",
        "agreement",
        "price",
        "value",
        "count",
        "transaction count",
        "latitude",
        "longitude",
        "metric",
        "rate",
    ]

    if any(term in text for term in bad_terms):
        return False

    return True


# ============================================================
# TOKEN LEDGER
# ============================================================

def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> Dict[str, float]:
    pricing = MODEL_PRICING_USD_PER_1M_TOKENS.get(model)
    if not pricing:
        return {
            "input_cost": 0.0,
            "cached_input_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
        }

    billable_input_tokens = max(input_tokens - cached_input_tokens, 0)
    input_cost = (billable_input_tokens / 1_000_000) * pricing["input"]
    cached_input_cost = (cached_input_tokens / 1_000_000) * pricing["cached_input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    return {
        "input_cost": round(input_cost, 8),
        "cached_input_cost": round(cached_input_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(input_cost + cached_input_cost + output_cost, 8),
    }


def extract_usage_from_response(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
        }

    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens) or (
        input_tokens + output_tokens
    )

    cached_input_tokens = 0
    input_details = getattr(usage, "input_tokens_details", None)
    if input_details:
        cached_input_tokens = getattr(input_details, "cached_tokens", 0) or 0

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }


def add_to_token_ledger(
    call_name: str,
    model: str,
    usage_data: Dict[str, int],
    cost_data: Dict[str, float],
) -> None:
    global _active_ledger
    if _active_ledger is not None:
        _active_ledger.add(call_name, model, usage_data, cost_data)


def get_token_ledger_summary() -> Dict[str, Any]:
    global _active_ledger
    if _active_ledger is not None:
        return _active_ledger.summary()
    return {
        "total_llm_calls": 0,
        "total_input_tokens": 0,
        "total_cached_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "ledger": [],
    }


# ============================================================
# STEP LOGGER
# ============================================================

class StepLogger:
    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []

    def add(
        self,
        step_name: str,
        status: str,
        changes_made: List[str],
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.steps.append(
            {
                "timestamp": now_str(),
                "step_name": step_name,
                "status": status,
                "changes_made": changes_made,
                "details": details or {},
            }
        )

    def to_list(self) -> List[Dict[str, Any]]:
        return self.steps


# ============================================================
# LLM WRAPPER
# ============================================================

def call_llm_json(
    call_name: str,
    system_prompt: str,
    user_prompt: str,
    fallback: Dict[str, Any],
) -> Dict[str, Any]:
    if not is_api_available():
        fallback["_llm_used"] = False
        fallback["_llm_note"] = "LLM skipped because OPENAI_API_KEY is blank or openai package is unavailable."
        return fallback

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        usage_data = extract_usage_from_response(response)
        cost_data = calculate_cost(
            model=OPENAI_MODEL,
            input_tokens=usage_data["input_tokens"],
            output_tokens=usage_data["output_tokens"],
            cached_input_tokens=usage_data["cached_input_tokens"],
        )

        add_to_token_ledger(
            call_name=call_name,
            model=OPENAI_MODEL,
            usage_data=usage_data,
            cost_data=cost_data,
        )

        result = extract_json_from_text(response.output_text)
        result["_llm_used"] = True
        result["_llm_call_name"] = call_name
        result["_llm_usage"] = usage_data
        result["_llm_cost"] = cost_data
        return result

    except Exception as exc:
        fallback["_llm_used"] = False
        fallback["_llm_error"] = str(exc)
        return fallback


# ============================================================
# DATA MAPPING SCHEMA PARSER
# ============================================================

def extract_string_constant_from_py(path: str, variable_name: str) -> Optional[str]:
    p = Path(path)
    source = p.read_text(encoding="utf-8")

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == variable_name:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


def parse_transaction_query_schema(schema_text: str) -> Dict[str, Any]:
    tables: Dict[str, Dict[str, str]] = {}
    current_table = None
    semantic_notes = []

    for raw_line in schema_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            continue

        if stripped.endswith(":") and not raw_line.startswith("  "):
            table_name = stripped[:-1].strip()
            current_table = table_name
            tables.setdefault(current_table, {})
            continue

        if current_table and raw_line.startswith("  ") and ":" in stripped:
            col, meaning = stripped.split(":", 1)
            col = col.strip()
            meaning = meaning.strip()
            if col and meaning:
                tables[current_table][col] = meaning
            continue

        if "Semantic category columns" in stripped or current_table == "Semantic category columns":
            semantic_notes.append(stripped)

    column_mapping = {}
    for table_name, cols in tables.items():
        if table_name.lower() == "semantic category columns":
            continue
        for col, meaning in cols.items():
            column_mapping[col] = meaning

    return {
        "raw_schema_text": schema_text,
        "tables": tables,
        "column_mapping": column_mapping,
        "semantic_notes": semantic_notes,
    }


def load_data_mapping_schema(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Data mapping file not found: {path}")

    schema_text = extract_string_constant_from_py(path, "TRANSACTION_QUERY_SCHEMA")

    if schema_text:
        parsed = parse_transaction_query_schema(schema_text)
        return {
            "mapping_type": "transaction_query_schema_text",
            "schema_text": schema_text,
            "parsed_schema": parsed,
            "column_mapping": parsed["column_mapping"],
        }

    namespace: Dict[str, Any] = {}
    exec(p.read_text(encoding="utf-8"), namespace)

    for key in ["data_mapping", "DATA_MAPPING", "mapping", "COLUMN_MAPPING", "column_mapping"]:
        if key in namespace and isinstance(namespace[key], dict):
            mapping = {str(k): str(v) for k, v in namespace[key].items()}
            return {
                "mapping_type": "dictionary",
                "schema_text": "",
                "parsed_schema": {},
                "column_mapping": mapping,
            }

    raise ValueError("No TRANSACTION_QUERY_SCHEMA string or dictionary mapping found in data_mapping.py.")


def empty_mapping_schema() -> Dict[str, Any]:
    return {
        "mapping_type": "not_used",
        "schema_text": "",
        "parsed_schema": {},
        "column_mapping": {},
    }


# ============================================================
# FILE LOADERS
# ============================================================

def load_json_file(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Text file not found: {path}")
    return p.read_text(encoding="utf-8")


def load_excel_data(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Excel file not found: {path}")
    df = pd.read_excel(p)
    df.columns = [normalize_column_name(c) for c in df.columns]
    return df


def default_module_1_intent() -> Dict[str, Any]:
    return {
        "module_number": 1,
        "module_name": "Intent Finalization & Visualization Planning",
        "user_query": "",
        "business_objective": "",
        "structured_intent": {},
        "map_output_requirements": {
            "is_active": True,
            "selected_map_types": ["2d_heatmap"],
            "primary_map_type": "2d_heatmap",
            "base_map_metric": "metric_value",
            "geo_level": "auto",
            "time_field_required": False,
            "time_granularity": None,
            "timelapse_required": False,
            "timelapse_mode": "time_slider",
            "additional_parameters": {},
        },
        "intent_mapping": {},
        "execution_flags": {},
        "required_modules": [],
    }


# ============================================================
# MODULE 1 INTENT EXTRACTION
# ============================================================

def deep_find_values(obj: Any, keys: List[str]) -> List[Any]:
    found = []
    key_norms = [normalize_text(k) for k in keys]

    if isinstance(obj, dict):
        for k, v in obj.items():
            if normalize_text(k) in key_norms:
                found.append(v)
            found.extend(deep_find_values(v, keys))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(deep_find_values(item, keys))

    return found


def extract_module_1_requirements(module_1: Dict[str, Any]) -> Dict[str, Any]:
    structured = ensure_dict(module_1.get("structured_intent"))
    map_req = ensure_dict(module_1.get("map_output_requirements"))
    additional = ensure_dict(map_req.get("additional_parameters"))

    locations = first_non_empty(
        structured.get("locations"),
        structured.get("location_focus"),
        additional.get("locations"),
        deep_find_values(structured, ["locations", "location_focus"]),
    )

    if locations and isinstance(locations, list):
        cleaned_locations = []
        for loc in locations:
            if isinstance(loc, dict):
                cleaned_locations.append(loc.get("name") or loc.get("value"))
            else:
                cleaned_locations.append(loc)
        locations = [str(x) for x in cleaned_locations if x]
    else:
        locations = ensure_list(locations)

    time_range = first_non_empty(
        structured.get("time_range"),
        structured.get("time_period"),
        additional.get("time_range"),
        additional.get("target_period"),
    )

    metric = first_non_empty(
        map_req.get("base_map_metric"),
        structured.get("metric"),
        structured.get("primary_metric"),
        structured.get("measure_type"),
        structured.get("topic"),
    )

    subject = first_non_empty(
        structured.get("subject"),
        structured.get("property_segment"),
        additional.get("entity_type"),
    )

    data_grain = first_non_empty(
        structured.get("data_grain"),
        module_1.get("data_grain"),
        additional.get("data_grain"),
        "transaction_level",
    )

    return {
        "user_query": module_1.get("user_query", ""),
        "business_objective": module_1.get("business_objective", ""),
        "metric": metric,
        "locations": locations,
        "subject": subject,
        "time_range": time_range,
        "geo_level": first_non_empty(map_req.get("geo_level"), structured.get("geo_focus"), structured.get("geography_level")),
        "primary_map_type": map_req.get("primary_map_type"),
        "selected_map_types": ensure_list(map_req.get("selected_map_types")),
        "time_field_required": bool(map_req.get("time_field_required")),
        "time_granularity": map_req.get("time_granularity"),
        "timelapse_required": bool(map_req.get("timelapse_required")),
        "timelapse_mode": map_req.get("timelapse_mode", "time_slider"),
        "layer_requirements": ensure_dict(map_req.get("layer_requirements")),
        "map_output_requirements": map_req,
        "intent_mapping": ensure_dict(module_1.get("intent_mapping")),
        "execution_flags": ensure_dict(module_1.get("execution_flags")),
        "required_modules": ensure_list(module_1.get("required_modules")),
        "data_grain": data_grain,
    }


def extract_year_range(time_range: Any) -> Tuple[Optional[int], Optional[int]]:
    if isinstance(time_range, dict):
        start = first_non_empty(time_range.get("start"), time_range.get("start_year"), time_range.get("from"))
        end = first_non_empty(time_range.get("end"), time_range.get("end_year"), time_range.get("to"))
        try:
            return int(start), int(end)
        except Exception:
            return None, None

    if isinstance(time_range, list) and len(time_range) >= 2:
        try:
            return int(time_range[0]), int(time_range[1])
        except Exception:
            return None, None

    years = re.findall(r"\b(?:19|20)\d{2}\b", str(time_range))
    if len(years) >= 2:
        return int(years[0]), int(years[-1])
    if len(years) == 1:
        return int(years[0]), int(years[0])
    return None, None


# ============================================================
# FIELD MAPPING
# ============================================================

STANDARD_FIELDS = {
    "geo_field": ["location", "locality", "village", "micromarket", "micro market", "sub locality", "region", "city"],
    "project_field": ["project", "project name", "rera project", "building", "tower"],
    "time_field": ["date", "year", "quarter", "transaction date", "agreement execution", "registration"],
    "property_type_field": ["property type", "asset type", "residential", "commercial", "project type", "unit configuration"],
    "metric_field": ["sales density", "density", "sales count", "transaction count", "count", "metric", "value"],
    "sales_value_field": ["agreement price", "sale value", "sales value", "transaction value", "guideline value", "price"],
    "latitude_field": ["latitude", "lat"],
    "longitude_field": ["longitude", "long", "lng", "lon"],
    "rate_field": ["rate", "price per sqft", "price per sq ft", "price per sqm", "unit rate"],
    "area_field": ["area", "carpet", "net carpet", "built", "sqft", "sqm", "sq m"],
    "sold_units_field": ["sold units", "units sold", "sold flat", "sold apartment"],
    "total_units_field": ["total units", "total flat", "total apartment", "inventory"],
}


def deterministic_field_mapping(df: pd.DataFrame, column_mapping: Dict[str, str]) -> Dict[str, Any]:
    mapped = {}
    mapping_reasons = {}
    confidence = {}
    available_cols = list(df.columns)

    for standard_field, keywords in STANDARD_FIELDS.items():
        best_col = None
        best_score = 0.0
        best_reason = ""

        for col in available_cols:
            meaning = column_mapping.get(col, "")
            combined = f"{col} {meaning}"
            combined_norm = normalize_text(combined)

            score = 0.0
            matched_keywords = []

            for kw in keywords:
                kw_norm = normalize_text(kw)
                if kw_norm in combined_norm:
                    score += 1.0
                    matched_keywords.append(kw)

                score = max(score, similarity(kw_norm, combined_norm) * 0.5)

            if standard_field == "geo_field":
                if is_numeric_series(df[col]):
                    score -= 2.0
                if any(term in normalize_text(f"{col} {meaning}") for term in ["carpet", "sq m", "sqft", "price", "count", "transaction"]):
                    score -= 2.0

            if score > best_score:
                best_score = score
                best_col = col
                best_reason = f"Matched keywords: {matched_keywords}; combined text: {combined}"

        if best_col and best_score >= 0.45:
            mapped[standard_field] = best_col
            mapping_reasons[standard_field] = best_reason
            confidence[standard_field] = round(min(best_score, 1.0), 3)
        else:
            mapped[standard_field] = None
            mapping_reasons[standard_field] = "No reliable deterministic match found."
            confidence[standard_field] = 0.0

    if not is_valid_geo_field(df, mapped.get("geo_field"), column_mapping):
        fallback_geo = choose_best_location_column(df, column_mapping)
        if fallback_geo:
            mapped["geo_field"] = fallback_geo
            mapping_reasons["geo_field"] = "Corrected geo_field using safe location-column fallback."
            confidence["geo_field"] = 0.95

    return {
        "mapped_fields": mapped,
        "mapping_reasons": mapping_reasons,
        "mapping_confidence": confidence,
    }


def llm_field_mapping(
    df: pd.DataFrame,
    mapping_schema: Dict[str, Any],
    requirements: Dict[str, Any],
    deterministic_mapping: Dict[str, Any],
    retrieval_context: Dict[str, Any],
    retrieval_sql_query: str,
    inputs_considered: Dict[str, bool],
) -> Dict[str, Any]:
    sample_rows = df.head(5).to_dict(orient="records")

    system = """
You are a data column mapping assistant for Module 2 of a real estate Visualization Agent.

You must map actual dataset columns to standard internal fields.

Return only valid JSON.

Standard fields:
- geo_field
- project_field
- time_field
- property_type_field
- metric_field
- sales_value_field
- latitude_field
- longitude_field
- rate_field
- area_field
- sold_units_field
- total_units_field

Important rules:
1. Use actual available dataset column names only.
2. If no column matches a standard field, set it to null.
3. Do not invent columns.
4. geo_field must be a location/name/geography column, not a numeric metric column.
5. geo_field must never be carpet area, agreement price, count, rate, latitude, or longitude.
6. If location_name exists, it is usually the best geo_field.
7. latitude_field and longitude_field are separate fields, not geo_field.
8. Provide confidence from 0 to 1.
9. Explain briefly why each mapped column was selected.
"""

    user_payload = {
        "available_columns": list(df.columns),
        "sample_rows": sample_rows,
        "module_1_requirements": requirements if inputs_considered.get("module_1_intent") else {},
        "deterministic_mapping": deterministic_mapping,
        "inputs_considered": inputs_considered,
    }

    if inputs_considered.get("data_mapping"):
        user_payload["schema_text"] = mapping_schema.get("schema_text", "")
        user_payload["parsed_column_mapping"] = mapping_schema.get("column_mapping", {})

    if inputs_considered.get("retrieval_model_intent"):
        user_payload["retrieval_context"] = retrieval_context

    if inputs_considered.get("retrieval_sql_query"):
        user_payload["retrieval_sql_query"] = retrieval_sql_query

    fallback = {
        "mapped_fields": deterministic_mapping.get("mapped_fields", {}),
        "confidence": deterministic_mapping.get("mapping_confidence", {}),
        "reasoning": deterministic_mapping.get("mapping_reasons", {}),
    }

    return call_llm_json(
        call_name="Column Mapping LLM",
        system_prompt=system,
        user_prompt=safe_json_dumps(user_payload),
        fallback=fallback,
    )


def merge_field_mappings(
    deterministic: Dict[str, Any],
    llm_result: Dict[str, Any],
    df: pd.DataFrame,
    column_mapping: Dict[str, str],
) -> Dict[str, Any]:
    det_fields = ensure_dict(deterministic.get("mapped_fields"))
    llm_fields = ensure_dict(llm_result.get("mapped_fields"))
    llm_conf = ensure_dict(llm_result.get("confidence"))
    det_conf = ensure_dict(deterministic.get("mapping_confidence"))

    final = {}
    final_sources = {}

    for field in STANDARD_FIELDS.keys():
        llm_col = llm_fields.get(field)
        det_col = det_fields.get(field)

        if field == "geo_field":
            if is_valid_geo_field(df, llm_col, column_mapping) and float(llm_conf.get(field, 0) or 0) >= 0.55:
                final[field] = llm_col
                final_sources[field] = "llm"
            elif is_valid_geo_field(df, det_col, column_mapping):
                final[field] = det_col
                final_sources[field] = "deterministic"
            else:
                fallback_geo = choose_best_location_column(df, column_mapping)
                final[field] = fallback_geo
                final_sources[field] = "safe_geo_fallback"
            continue

        if llm_col in df.columns and float(llm_conf.get(field, 0) or 0) >= 0.55:
            final[field] = llm_col
            final_sources[field] = "llm"
        elif det_col in df.columns:
            final[field] = det_col
            final_sources[field] = "deterministic"
        elif llm_col in df.columns:
            final[field] = llm_col
            final_sources[field] = "llm_low_confidence"
        else:
            final[field] = None
            final_sources[field] = "unmapped"

    return {
        "mapped_fields": final,
        "mapping_source": final_sources,
        "deterministic_mapping": deterministic,
        "llm_mapping": llm_result,
        "deterministic_confidence": det_conf,
    }


def find_existing_column(df: pd.DataFrame, candidate_names: List[str]) -> Optional[str]:
    exact_columns = {str(col): col for col in df.columns}
    normalized_columns = {normalize_text(col): col for col in df.columns}

    for candidate in candidate_names:
        if not candidate:
            continue
        if candidate in exact_columns:
            return exact_columns[candidate]
        normalized_candidate = normalize_text(candidate)
        if normalized_candidate in normalized_columns:
            return normalized_columns[normalized_candidate]

    return None


def repair_time_field_mapping(
    df: pd.DataFrame,
    mapped_fields: Dict[str, Any],
    requirements: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    repaired = dict(mapped_fields)
    original_time_field = repaired.get("time_field")

    if not requirements.get("time_field_required"):
        return repaired, {
            "changed": False,
            "reason": "time_field_required is false.",
            "original_time_field": original_time_field,
            "selected_time_field": original_time_field,
        }

    granularity = normalize_text(requirements.get("time_granularity") or "annual")
    original_exists = original_time_field in df.columns if original_time_field else False

    if granularity in ["annual", "year", "yearly"]:
        preferred = [
            "year",
            "transaction_year",
            "registration_year",
            "agreement_year",
            "sale_year",
            "transaction_date",
            "registration_date",
            "agreement_date",
            "date",
            original_time_field,
        ]
    elif granularity in ["quarterly", "quarter", "qtr"]:
        preferred = [
            "quarter",
            "year_quarter",
            "transaction_quarter",
            "qtr",
            "transaction_date",
            "registration_date",
            "agreement_date",
            "date",
            original_time_field,
            "year",
        ]
    elif granularity in ["monthly", "month"]:
        preferred = [
            "month",
            "year_month",
            "transaction_month",
            "transaction_date",
            "registration_date",
            "agreement_date",
            "date",
            original_time_field,
            "year",
        ]
    else:
        preferred = [
            original_time_field,
            "year",
            "quarter",
            "month",
            "transaction_date",
            "registration_date",
            "agreement_date",
            "date",
        ]

    selected = find_existing_column(df, [name for name in preferred if name])
    if not selected and original_exists:
        selected = original_time_field

    if selected:
        repaired["time_field"] = selected

    return repaired, {
        "changed": selected != original_time_field,
        "reason": "Selected the time column that best matches Module 1 time granularity.",
        "time_granularity": granularity,
        "original_time_field": original_time_field,
        "selected_time_field": selected,
        "available_columns": list(df.columns),
    }


# ============================================================
# VALIDATION + FILTERING
# ============================================================

RESIDENTIAL_TERMS = ["residential", "flat", "apartment", "unit", "dwelling", "housing"]
COMMERCIAL_TERMS = ["commercial", "office", "shop", "retail", "showroom"]


def required_fields_for_intent(requirements: Dict[str, Any], mapped_fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    required = []

    if requirements.get("locations"):
        required.append(
            {
                "standard_field": "geo_field",
                "required_for": ["location filtering", "geo aggregation", "map plotting"],
                "reason": "Module 1 has location-specific intent.",
            }
        )

    if requirements.get("time_field_required"):
        required.append(
            {
                "standard_field": "time_field",
                "required_for": ["time filtering", "time aggregation", "timelapse_frame creation"],
                "reason": "Module 1 marked time_field_required=true.",
            }
        )

    metric = normalize_text(requirements.get("metric"))
    if metric:
        required.append(
            {
                "standard_field": "metric_or_calculable_fields",
                "required_for": ["metric calculation", "visualization intensity", "aggregation"],
                "reason": f"Module 1 requires metric: {requirements.get('metric')}",
            }
        )

    missing = []
    for item in required:
        field = item["standard_field"]
        if field == "metric_or_calculable_fields":
            continue
        if not mapped_fields.get(field):
            missing.append(item)

    return missing


def llm_missing_explainer(
    missing: List[Dict[str, Any]],
    df: pd.DataFrame,
    mapping_schema: Dict[str, Any],
    requirements: Dict[str, Any],
    inputs_considered: Dict[str, bool],
) -> Dict[str, Any]:
    system = """
You are a missing requirement explainer for Module 2.

Return only valid JSON.

Explain:
- Missing field
- Why it is required
- Which downstream module depends on it
- What data should be requested later from the Data Retrieval Agent
"""

    user = {
        "missing_required_fields": missing,
        "available_columns": list(df.columns),
        "module_1_requirements": requirements if inputs_considered.get("module_1_intent") else {},
        "inputs_considered": inputs_considered,
    }

    if inputs_considered.get("data_mapping"):
        user["schema_text"] = mapping_schema.get("schema_text", "")
        user["parsed_column_mapping"] = mapping_schema.get("column_mapping", {})

    fallback = {
        "missing_required_fields": missing,
        "available_columns": list(df.columns),
        "suggested_resolution": "Provide missing columns from Data Retrieval Agent or include them through retrieval_context.",
    }

    return call_llm_json(
        call_name="Missing Requirement Explainer LLM",
        system_prompt=system,
        user_prompt=safe_json_dumps(user),
        fallback=fallback,
    )


def create_missing_fields_output(
    missing: List[Dict[str, Any]],
    df: pd.DataFrame,
    mapping_schema: Dict[str, Any],
    requirements: Dict[str, Any],
    logger: StepLogger,
    inputs_considered: Dict[str, bool],
) -> Dict[str, Any]:
    explanation = llm_missing_explainer(missing, df, mapping_schema, requirements, inputs_considered)

    logger.add(
        "Generate Missing Field JSON",
        "missing_required_fields",
        [
            "Created structured missing-field output.",
            "Included available columns and requirement reasons.",
            "Stopped before filtering/aggregation because minimum required fields are unavailable.",
        ],
        explanation,
    )

    return {
        "module_number": 2,
        "module_name": "Data Restructuring & Filtering",
        "status": "missing_required_fields",
        "next_module_ready": False,
        "inputs_considered": inputs_considered,
        "missing_required_fields": explanation.get("missing_required_fields", missing),
        "available_columns": list(df.columns),
        "available_mapping": mapping_schema.get("column_mapping", {}),
        "data_quality_summary": {
            "rows_received": len(df),
            "columns_received": list(df.columns),
            "warnings": ["Required fields are missing. Analysis-ready dataset could not be finalized."],
        },
        "debug_metadata": {
            "step_log": logger.to_list(),
            "llm_token_ledger": get_token_ledger_summary(),
        },
    }


def match_location_series(series: pd.Series, target_locations: List[str]) -> pd.Series:
    norm_series = series.astype(str).map(normalize_text)
    mask = pd.Series(False, index=series.index)

    for loc in target_locations:
        loc_norm = normalize_text(loc)
        if not loc_norm:
            continue

        exact_or_contains = norm_series.str.contains(re.escape(loc_norm), na=False)
        reverse_contains = norm_series.map(lambda x: loc_norm in x or x in loc_norm if x else False)
        fuzzy = norm_series.map(lambda x: similarity(x, loc_norm) >= 0.78 if x else False)

        mask = mask | exact_or_contains | reverse_contains | fuzzy

    return mask


def apply_missing_filters(
    df: pd.DataFrame,
    requirements: Dict[str, Any],
    mapped_fields: Dict[str, Any],
    retrieval_context: Dict[str, Any],
    logger: StepLogger,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    original_rows = len(df)
    filters_applied = {}
    filters_validated = {}
    warnings = []

    result = df.copy()

    locations = ensure_list(requirements.get("locations"))
    geo_field = mapped_fields.get("geo_field")

    if locations and geo_field and geo_field in result.columns:
        before = len(result)
        mask = match_location_series(result[geo_field], locations)
        matched_rows = int(mask.sum())

        if matched_rows > 0 and matched_rows < before:
            result = result[mask].copy()
            filters_applied["location_filter"] = {
                "field": geo_field,
                "values": locations,
                "rows_before": before,
                "rows_after": len(result),
                "method": "case_insensitive_contains_or_basic_fuzzy",
            }
        else:
            filters_validated["location_filter"] = {
                "field": geo_field,
                "values": locations,
                "status": "already_satisfied_or_no_extra_filter_needed",
                "matched_rows": matched_rows,
            }

    subject_text = normalize_text(requirements.get("subject")) + " " + normalize_text(requirements.get("metric"))
    property_field = mapped_fields.get("property_type_field")

    if "residential" in subject_text and property_field and property_field in result.columns:
        before = len(result)
        norm_prop = result[property_field].astype(str).map(normalize_text)
        mask = norm_prop.map(lambda x: any(term in x for term in RESIDENTIAL_TERMS))
        matched_rows = int(mask.sum())

        if matched_rows > 0 and matched_rows < before:
            result = result[mask].copy()
            filters_applied["property_segment_filter"] = {
                "field": property_field,
                "values": RESIDENTIAL_TERMS,
                "rows_before": before,
                "rows_after": len(result),
            }
        else:
            filters_validated["property_segment_filter"] = {
                "field": property_field,
                "status": "already_satisfied_or_no_extra_filter_needed",
                "matched_rows": matched_rows,
            }

    logger.add(
        "Validate and Apply Missing Filters",
        "success",
        [
            f"Started with {original_rows} rows.",
            f"Ended with {len(result)} rows after applying only missing filters.",
            "Location matching used exact/contains/basic fuzzy logic where applicable.",
            "Property segment filter applied only when required and available.",
        ],
        {
            "filters_applied_by_module_2": filters_applied,
            "filters_validated": filters_validated,
            "warnings": warnings,
        },
    )

    return result, {
        "filters_applied_by_module_2": filters_applied,
        "filters_validated": filters_validated,
        "warnings": warnings,
    }


# ============================================================
# TIME HANDLING
# ============================================================

def create_time_fields_legacy(
    df: pd.DataFrame,
    requirements: Dict[str, Any],
    mapped_fields: Dict[str, Any],
    logger: StepLogger,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    result = df.copy()
    time_field = mapped_fields.get("time_field")
    time_required = requirements.get("time_field_required")
    granularity = normalize_text(requirements.get("time_granularity") or "annual")
    timelapse_required = bool(requirements.get("timelapse_required"))

    changes = []
    warnings = []

    if not time_required:
        logger.add(
            "Create Time Fields",
            "skipped",
            ["time_field_required is false, so no time fields were created."],
        )
        return result, {"warnings": warnings}

    if not time_field or time_field not in result.columns:
        warnings.append("Time field is required but not mapped or not available.")
        logger.add(
            "Create Time Fields",
            "warning",
            ["Time field could not be created because mapped time field is missing."],
            {"warnings": warnings},
        )
        return result, {"warnings": warnings}

    year_like_ratio = result[time_field].dropna().astype(str).str.match(r"^\d{4}$").mean()

    if normalize_text(time_field) == "year" or year_like_ratio > 0.7:
        result["year"] = pd.to_numeric(result[time_field], errors="coerce").astype("Int64")
        changes.append("Created year from existing year-like field.")
    else:
        dt = pd.to_datetime(result[time_field], errors="coerce", dayfirst=True)
        result["_parsed_time"] = dt
        result["year"] = dt.dt.year.astype("Int64")
        result["month"] = dt.dt.month.astype("Int64")
        result["quarter_number"] = dt.dt.quarter.astype("Int64")
        result["quarter"] = "Q" + result["quarter_number"].astype(str) + "-" + result["year"].astype(str)
        changes.append(f"Parsed {time_field} as datetime and created year/month/quarter.")

    start_year, end_year = extract_year_range(requirements.get("time_range"))
    if start_year and end_year and "year" in result.columns:
        before = len(result)
        result = result[(result["year"] >= start_year) & (result["year"] <= end_year)].copy()
        changes.append(f"Applied time filter from {start_year} to {end_year}; rows {before} → {len(result)}.")

    if timelapse_required:
        if granularity in ["annual", "year", "yearly"]:
            result["timelapse_frame"] = result["year"].astype(str)
            changes.append("Created timelapse_frame using annual year values.")
        elif granularity in ["quarterly", "quarter"]:
            if "quarter" not in result.columns:
                result["quarter"] = "Q" + result["quarter_number"].astype(str) + "-" + result["year"].astype(str)
            result["timelapse_frame"] = result["quarter"].astype(str)
            changes.append("Created timelapse_frame using quarter values.")
        elif granularity in ["monthly", "month"]:
            result["timelapse_frame"] = result["year"].astype(str) + "-" + result["month"].astype(str).str.zfill(2)
            changes.append("Created timelapse_frame using monthly values.")
        else:
            result["timelapse_frame"] = result["year"].astype(str)
            changes.append("Created timelapse_frame using default year values.")

    logger.add(
        "Create Time Fields and Timelapse Frame",
        "success",
        changes,
        {
            "time_field": time_field,
            "time_granularity": granularity,
            "timelapse_required": timelapse_required,
            "warnings": warnings,
        },
    )

    return result, {"warnings": warnings, "time_field": time_field}


def create_time_fields(
    df: pd.DataFrame,
    requirements: Dict[str, Any],
    mapped_fields: Dict[str, Any],
    logger: StepLogger,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    result = df.copy()
    time_field = mapped_fields.get("time_field")
    time_required = requirements.get("time_field_required")
    granularity = normalize_text(requirements.get("time_granularity") or "annual")
    timelapse_required = bool(requirements.get("timelapse_required"))

    changes = []
    warnings = []
    time_filter_validation = {
        "filters_applied_by_module_2": {},
        "filters_validated": {},
        "warnings": [],
    }

    if not time_required:
        logger.add(
            "Create Time Fields",
            "skipped",
            ["time_field_required is false, so no time fields were created."],
        )
        return result, {"warnings": warnings, "filter_validation": time_filter_validation}

    if not time_field or time_field not in result.columns:
        warning = "Time field is required but not mapped or not available."
        warnings.append(warning)
        time_filter_validation["warnings"].append("Time filtering could not run because the time field is missing.")
        logger.add(
            "Create Time Fields",
            "warning",
            ["Time field could not be created because mapped time field is missing."],
            {"warnings": warnings},
        )
        return result, {"warnings": warnings, "filter_validation": time_filter_validation}

    time_values = result[time_field].dropna().astype(str).str.strip()
    year_like_ratio = time_values.str.match(r"^\d{4}$").mean() if len(time_values) else 0
    quarter_like_ratio = (
        time_values.str.match(r"(?i)^q[1-4][\s\-/]*(?:19|20)\d{2}$").mean()
        if len(time_values)
        else 0
    )
    normalized_time_field = normalize_text(time_field)

    if normalized_time_field == "year" or year_like_ratio > 0.7:
        result["year"] = pd.to_numeric(result[time_field], errors="coerce").astype("Int64")
        changes.append("Created year from existing year-like field.")
    elif normalized_time_field in ["quarter", "qtr", "year quarter"] or quarter_like_ratio > 0.7:
        quarter_text = result[time_field].astype(str).str.strip()
        year_match = quarter_text.str.extract(r"((?:19|20)\d{2})", expand=False)
        quarter_match = quarter_text.str.extract(r"(?i)(q[1-4])", expand=False)
        quarter_number = quarter_match.str.extract(r"([1-4])", expand=False)

        result["year"] = pd.to_numeric(year_match, errors="coerce").astype("Int64")
        result["quarter_number"] = pd.to_numeric(quarter_number, errors="coerce").astype("Int64")
        result["quarter"] = quarter_text.str.upper().str.replace(r"\s+", "", regex=True).str.replace("/", "-", regex=False)

        valid_quarter = result["year"].notna() & result["quarter_number"].notna()
        canonical_quarter = "Q" + result["quarter_number"].astype(str) + "-" + result["year"].astype(str)
        result.loc[valid_quarter, "quarter"] = canonical_quarter[valid_quarter]
        changes.append("Read quarter-like values and created year/quarter fields without datetime parsing.")
    else:
        dt = pd.to_datetime(result[time_field], errors="coerce", dayfirst=True)
        result["_parsed_time"] = dt
        result["year"] = dt.dt.year.astype("Int64")
        result["month"] = dt.dt.month.astype("Int64")
        result["quarter_number"] = dt.dt.quarter.astype("Int64")
        result["quarter"] = "Q" + result["quarter_number"].astype(str) + "-" + result["year"].astype(str)
        changes.append(f"Parsed {time_field} as datetime and created year/month/quarter.")

    start_year, end_year = extract_year_range(requirements.get("time_range"))
    if (start_year or end_year) and "year" in result.columns and result["year"].notna().any():
        before = len(result)
        mask = pd.Series(True, index=result.index)
        if start_year:
            mask &= result["year"] >= start_year
        if end_year:
            mask &= result["year"] <= end_year
        result = result[mask].copy()
        time_filter_payload = {
            "field": time_field,
            "start_year": start_year,
            "end_year": end_year,
            "rows_before": before,
            "rows_after": len(result),
        }
        if len(result) != before:
            time_filter_validation["filters_applied_by_module_2"]["time_range_filter"] = time_filter_payload
        else:
            time_filter_validation["filters_validated"]["time_range_filter"] = {
                **time_filter_payload,
                "status": "already_satisfied_or_no_extra_filter_needed",
            }
        changes.append(f"Applied time filter from {start_year} to {end_year}; rows {before} -> {len(result)}.")
    elif start_year or end_year:
        warning = "Time range was requested but no usable year values were available for filtering."
        warnings.append(warning)
        time_filter_validation["warnings"].append(warning)

    if timelapse_required:
        if granularity in ["annual", "year", "yearly"]:
            if "year" in result.columns and result["year"].notna().any():
                result["timelapse_frame"] = result["year"].astype(str)
                changes.append("Created timelapse_frame using annual year values.")
            else:
                warnings.append("Timelapse was requested but annual year values are unavailable.")
        elif granularity in ["quarterly", "quarter"]:
            if "quarter" not in result.columns and "quarter_number" in result.columns and "year" in result.columns:
                result["quarter"] = "Q" + result["quarter_number"].astype(str) + "-" + result["year"].astype(str)
            if "quarter" in result.columns:
                result["timelapse_frame"] = result["quarter"].astype(str)
                changes.append("Created timelapse_frame using quarter values.")
            else:
                warnings.append("Timelapse was requested but quarter values are unavailable.")
        elif granularity in ["monthly", "month"]:
            if {"year", "month"}.issubset(result.columns):
                result["timelapse_frame"] = result["year"].astype(str) + "-" + result["month"].astype(str).str.zfill(2)
                changes.append("Created timelapse_frame using monthly values.")
            else:
                warnings.append("Timelapse was requested but monthly values are unavailable.")
        elif "year" in result.columns:
            result["timelapse_frame"] = result["year"].astype(str)
            changes.append("Created timelapse_frame using default year values.")

    logger.add(
        "Create Time Fields and Timelapse Frame",
        "success" if not warnings else "warning",
        changes or ["No time-field changes were made."],
        {
            "time_field": time_field,
            "time_granularity": granularity,
            "timelapse_required": timelapse_required,
            "filter_validation": time_filter_validation,
            "warnings": warnings,
        },
    )

    return result, {
        "warnings": warnings,
        "time_field": time_field,
        "filter_validation": time_filter_validation,
    }


# ============================================================
# METRIC + TRANSFORMATION PLAN
# ============================================================

def llm_transformation_plan(
    df: pd.DataFrame,
    requirements: Dict[str, Any],
    mapped_fields: Dict[str, Any],
    retrieval_context: Dict[str, Any],
    mapping_schema: Dict[str, Any],
    retrieval_sql_query: str,
    inputs_considered: Dict[str, bool],
) -> Dict[str, Any]:
    system = """
You are a transformation planner for Module 2 of a real estate Visualization Agent.

Return only valid JSON.

Do not write executable Python code.
Produce safe transformation instructions only.

Allowed operations:
- use_existing_metric
- count_rows
- sum_column
- mean_column
- group_by
- create_proxy_metric
- create_percentage_change
- cannot_calculate

Return:
{
  "metric_strategy": "",
  "metric_name": "",
  "operation": "",
  "group_by": [],
  "source_columns": [],
  "reason": "",
  "confidence": 0.0,
  "missing_requirements": []
}
"""

    user = {
        "available_columns": list(df.columns),
        "sample_rows": df.head(5).to_dict(orient="records"),
        "requirements": requirements if inputs_considered.get("module_1_intent") else {},
        "mapped_fields": mapped_fields,
        "inputs_considered": inputs_considered,
    }

    if inputs_considered.get("retrieval_model_intent"):
        user["retrieval_context"] = retrieval_context

    if inputs_considered.get("data_mapping"):
        user["schema_text"] = mapping_schema.get("schema_text", "")
        user["parsed_column_mapping"] = mapping_schema.get("column_mapping", {})

    if inputs_considered.get("retrieval_sql_query"):
        user["retrieval_sql_query"] = retrieval_sql_query

    fallback = {
        "metric_strategy": "deterministic_fallback",
        "metric_name": requirements.get("metric") or "metric_value",
        "operation": "auto",
        "group_by": [],
        "source_columns": [],
        "reason": "LLM unavailable; deterministic logic will be used.",
        "confidence": 0.5,
        "missing_requirements": [],
    }

    return call_llm_json(
        call_name="Transformation Plan LLM",
        system_prompt=system,
        user_prompt=safe_json_dumps(user),
        fallback=fallback,
    )


def find_existing_metric_column(df: pd.DataFrame, requirements: Dict[str, Any], mapped_fields: Dict[str, Any]) -> Optional[str]:
    metric = normalize_text(requirements.get("metric"))
    base_metric = normalize_text(requirements.get("map_output_requirements", {}).get("base_map_metric"))

    candidates = []
    if mapped_fields.get("metric_field"):
        candidates.append(mapped_fields["metric_field"])

    for col in df.columns:
        norm_col = normalize_text(col)
        if metric and (metric in norm_col or norm_col in metric):
            candidates.append(col)
        if base_metric and (base_metric in norm_col or norm_col in base_metric):
            candidates.append(col)
        if any(term in norm_col for term in ["sales density", "density", "sales count", "transaction count", "transaction_count"]):
            candidates.append(col)

    for col in candidates:
        if col in df.columns:
            return col
    return None


def build_group_fields(requirements: Dict[str, Any], mapped_fields: Dict[str, Any]) -> List[str]:
    group_fields = []

    geo_field = mapped_fields.get("geo_field")
    if geo_field:
        group_fields.append(geo_field)

    if requirements.get("timelapse_required"):
        group_fields.append("timelapse_frame")
    elif requirements.get("time_field_required"):
        group_fields.append("year")

    return [g for g in group_fields if g]


def calculate_metric_and_aggregate(
    df: pd.DataFrame,
    requirements: Dict[str, Any],
    mapped_fields: Dict[str, Any],
    retrieval_context: Dict[str, Any],
    mapping_schema: Dict[str, Any],
    retrieval_sql_query: str,
    inputs_considered: Dict[str, bool],
    logger: StepLogger,
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    metric = normalize_text(requirements.get("metric"))
    data_grain = normalize_text(requirements.get("data_grain") or "transaction_level")
    group_fields = build_group_fields(requirements, mapped_fields)
    existing_metric_col = find_existing_metric_column(df, requirements, mapped_fields)

    transformation_plan = llm_transformation_plan(
        df,
        requirements,
        mapped_fields,
        retrieval_context,
        mapping_schema,
        retrieval_sql_query,
        inputs_considered,
    )

    if not group_fields:
        missing_logic = {
            "status": "missing_grouping_logic",
            "reason": "No geography or time grouping field available for aggregation.",
            "required_data": ["geo field", "time field if time-aware output is required"],
            "transformation_plan": transformation_plan,
        }
        logger.add(
            "Check Metric and Aggregation Logic",
            "missing_logic",
            ["Could not determine grouping fields for aggregation."],
            missing_logic,
        )
        return None, missing_logic

    result = df.copy()
    agg_summary: Dict[str, Any] = {
        "aggregation_required": True,
        "aggregation_performed": False,
        "group_by": group_fields,
        "metric_name": requirements.get("metric") or "metric_value",
        "metric_logic": "",
        "derived_fields_created": [],
        "llm_transformation_plan": transformation_plan,
    }

    lat_field = mapped_fields.get("latitude_field")
    lon_field = mapped_fields.get("longitude_field")
    sales_value_field = mapped_fields.get("sales_value_field")
    rate_field = mapped_fields.get("rate_field")
    sold_units_field = mapped_fields.get("sold_units_field")

    agg_dict = {}

    if lat_field and lat_field in result.columns:
        result[lat_field] = pd.to_numeric(result[lat_field], errors="coerce")
        agg_dict[lat_field] = "mean"

    if lon_field and lon_field in result.columns:
        result[lon_field] = pd.to_numeric(result[lon_field], errors="coerce")
        agg_dict[lon_field] = "mean"

    if existing_metric_col and existing_metric_col in result.columns:
        result[existing_metric_col] = pd.to_numeric(result[existing_metric_col], errors="coerce")
        agg_dict[existing_metric_col] = "sum"
        metric_col_after = existing_metric_col
        agg_summary["metric_logic"] = f"Used existing metric column: {existing_metric_col}"

    elif "density" in metric or "sales" in metric:
        if data_grain == "transaction_level":
            result["_record_count_for_metric"] = 1
            agg_dict["_record_count_for_metric"] = "sum"
            metric_col_after = "_record_count_for_metric"
            agg_summary["metric_logic"] = "Calculated sales density proxy by counting transaction-level rows."
            agg_summary["derived_fields_created"].append("sales_density_proxy")

        elif data_grain == "project_level":
            if sold_units_field and sold_units_field in result.columns:
                result[sold_units_field] = pd.to_numeric(result[sold_units_field], errors="coerce")
                agg_dict[sold_units_field] = "sum"
                metric_col_after = sold_units_field
                agg_summary["metric_logic"] = "Used sold units as project-level sales metric."
            elif sales_value_field and sales_value_field in result.columns:
                result[sales_value_field] = pd.to_numeric(result[sales_value_field], errors="coerce")
                agg_dict[sales_value_field] = "sum"
                metric_col_after = sales_value_field
                agg_summary["metric_logic"] = "Used sales value as project-level fallback metric."
            else:
                missing_logic = {
                    "status": "missing_metric_logic",
                    "reason": "Project-level data does not contain a calculable sales density metric or fallback fields.",
                    "required_data": ["sold_units", "sales_value", "sales_density", "transaction_count"],
                    "available_columns": list(df.columns),
                    "transformation_plan": transformation_plan,
                }
                logger.add(
                    "Check Metric Availability",
                    "missing_logic",
                    ["Metric could not be calculated for project-level data."],
                    missing_logic,
                )
                return None, missing_logic
        else:
            result["_record_count_for_metric"] = 1
            agg_dict["_record_count_for_metric"] = "sum"
            metric_col_after = "_record_count_for_metric"
            agg_summary["metric_logic"] = "Unknown data grain; used row count as proxy."

    elif sales_value_field and sales_value_field in result.columns:
        result[sales_value_field] = pd.to_numeric(result[sales_value_field], errors="coerce")
        agg_dict[sales_value_field] = "sum"
        metric_col_after = sales_value_field
        agg_summary["metric_logic"] = f"Used sales value column: {sales_value_field}"

    elif rate_field and rate_field in result.columns:
        result[rate_field] = pd.to_numeric(result[rate_field], errors="coerce")
        agg_dict[rate_field] = "mean"
        metric_col_after = rate_field
        agg_summary["metric_logic"] = f"Used average rate column: {rate_field}"

    else:
        missing_logic = {
            "status": "missing_metric_logic",
            "reason": "Required metric is not available and cannot be calculated from available mapped fields.",
            "required_metric": requirements.get("metric"),
            "available_columns": list(df.columns),
            "mapped_fields": mapped_fields,
            "transformation_plan": transformation_plan,
        }
        logger.add(
            "Check Metric Availability",
            "missing_logic",
            ["No existing or calculable metric was found."],
            missing_logic,
        )
        return None, missing_logic

    aggregated = result.groupby(group_fields, dropna=False).agg(agg_dict).reset_index()

    aggregated["metric_name"] = requirements.get("metric") or "metric_value"
    aggregated["metric_value"] = pd.to_numeric(aggregated[metric_col_after], errors="coerce")

    if metric_col_after == "_record_count_for_metric":
        aggregated["sales_count"] = aggregated["metric_value"]
        aggregated["sales_density_proxy"] = aggregated["metric_value"]
        agg_summary["derived_fields_created"].extend(["sales_count", "sales_density_proxy"])

    geo_field = mapped_fields.get("geo_field")
    if geo_field and geo_field in aggregated.columns:
        aggregated["geo_label"] = aggregated[geo_field].astype(str)
    else:
        aggregated["geo_label"] = ""

    if "timelapse_frame" in aggregated.columns:
        aggregated["time_period"] = aggregated["timelapse_frame"].astype(str)
    elif "year" in aggregated.columns:
        aggregated["time_period"] = aggregated["year"].astype(str)
        if requirements.get("timelapse_required"):
            aggregated["timelapse_frame"] = aggregated["year"].astype(str)
    else:
        aggregated["time_period"] = ""

    if lat_field and lat_field in aggregated.columns:
        aggregated["latitude"] = aggregated[lat_field]
    else:
        aggregated["latitude"] = None

    if lon_field and lon_field in aggregated.columns:
        aggregated["longitude"] = aggregated[lon_field]
    else:
        aggregated["longitude"] = None

    def _tooltip_value(value: Any) -> Any:
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                return value
        return value

    def _tooltip_payload(row: pd.Series) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
        for key, value in row.items():
            if str(key).startswith("_"):
                continue
            safe_value = _tooltip_value(value)
            if safe_value in [None, ""]:
                continue
            fields[str(key)] = safe_value
            if len(fields) >= 24:
                break
        return {
            "geo_label": row.get("geo_label"),
            "time_period": row.get("time_period"),
            "metric_name": row.get("metric_name"),
            "metric_value": row.get("metric_value"),
            "fields": fields,
        }

    aggregated["tooltip_data"] = aggregated.apply(_tooltip_payload, axis=1)

    agg_summary["aggregation_performed"] = True

    logger.add(
        "Calculate Metric and Aggregate Data",
        "success",
        [
            f"Used group fields: {group_fields}",
            f"Metric logic: {agg_summary['metric_logic']}",
            f"Created {len(aggregated)} aggregated rows.",
            "Created standardized metric_value, metric_name, geo_label, time_period, and tooltip_data fields.",
        ],
        agg_summary,
    )

    return aggregated, agg_summary


# ============================================================
# VISUALIZATION OUTPUT
# ============================================================

def llm_visualization_structure_advisor(
    analysis_df: pd.DataFrame,
    requirements: Dict[str, Any],
    inputs_considered: Dict[str, bool],
) -> Dict[str, Any]:
    system = """
You are a visualization structure advisor for Module 2.

Return only valid JSON.

Given the selected visualization type and available analysis-ready columns,
advise what structure Module 3 should receive.

Do not invent unavailable columns.
"""

    user = {
        "selected_map_types": requirements.get("selected_map_types"),
        "primary_map_type": requirements.get("primary_map_type"),
        "timelapse_required": requirements.get("timelapse_required"),
        "available_columns": list(analysis_df.columns),
        "sample_records": analysis_df.head(5).to_dict(orient="records"),
        "inputs_considered": inputs_considered,
    }

    fallback = {
        "visualization_type": requirements.get("primary_map_type"),
        "required_fields": ["geo_label", "metric_value"],
        "recommended_fields": (
            ["time_period", "timelapse_frame", "latitude", "longitude", "floor_number", "floor_label", "floor_level", "tooltip_data"]
            if requirements.get("primary_map_type") == "3d_floor_wise"
            else ["time_period", "timelapse_frame", "latitude", "longitude", "tooltip_data"]
        ),
        "notes": "LLM unavailable; standard visualization-ready structure will be used.",
    }

    return call_llm_json(
        call_name="Visualization Structure Advisor LLM",
        system_prompt=system,
        user_prompt=safe_json_dumps(user),
        fallback=fallback,
    )


def build_visualization_ready_output(
    analysis_df: pd.DataFrame,
    requirements: Dict[str, Any],
    inputs_considered: Dict[str, bool],
    logger: StepLogger,
) -> Dict[str, Any]:
    advisor = llm_visualization_structure_advisor(analysis_df, requirements, inputs_considered)

    def _clean_record_value(value: Any) -> Any:
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                return str(value)
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                return value
        return value

    records_source = analysis_df.copy()
    records_source["raw_fields"] = records_source.apply(
        lambda row: {
            str(key): _clean_record_value(value)
            for key, value in row.to_dict().items()
            if not str(key).startswith("_")
        },
        axis=1,
    )

    standard_cols = [
        "geo_label",
        "time_period",
        "timelapse_frame",
        "floor_number",
        "floor_label",
        "floor_level",
        "tower_name",
        "building_name",
        "metric_name",
        "metric_value",
        "latitude",
        "longitude",
        "tooltip_data",
        "raw_fields",
    ]

    available_standard_cols = [c for c in standard_cols if c in records_source.columns]
    records = records_source[available_standard_cols].copy()

    output = {
        "visualization_type": requirements.get("primary_map_type"),
        "selected_map_types": requirements.get("selected_map_types"),
        "timelapse_required": requirements.get("timelapse_required"),
        "timelapse_mode": requirements.get("timelapse_mode"),
        "records": records.to_dict(orient="records"),
        "available_standard_columns": available_standard_cols,
        "advisor_output": advisor,
    }

    logger.add(
        "Build Visualization-Ready Output",
        "success",
        [
            f"Prepared output for visualization type: {requirements.get('primary_map_type')}",
            f"Included standard columns: {available_standard_cols}",
            f"Prepared {len(records)} visualization-ready records.",
        ],
        output,
    )

    return output


def validate_map_readiness(
    visualization_output: Dict[str, Any],
    requirements: Dict[str, Any],
    mapped_fields: Dict[str, Any],
    logger: StepLogger,
) -> Dict[str, Any]:
    records = ensure_list(visualization_output.get("records"))
    missing_for_direct_plotting = []
    blocking_missing = []
    warnings = []
    needs_geo_enrichment = False

    if not records:
        blocking_missing.append("visualization_ready_output.records")

    geo_values = [record.get("geo_label") for record in records if isinstance(record, dict)]
    non_empty_geo_values = [value for value in geo_values if str(value or "").strip()]
    if records and not non_empty_geo_values:
        blocking_missing.append("geo_label")

    if non_empty_geo_values:
        numeric_geo_count = sum(
            pd.to_numeric(pd.Series([value]), errors="coerce").notna().iloc[0]
            for value in non_empty_geo_values
        )
        if numeric_geo_count == len(non_empty_geo_values):
            blocking_missing.append("geo_label_non_numeric")

    metric_values = [record.get("metric_value") for record in records if isinstance(record, dict)]
    numeric_metric_values = pd.to_numeric(pd.Series(metric_values), errors="coerce") if metric_values else pd.Series(dtype=float)
    if records and numeric_metric_values.notna().sum() == 0:
        blocking_missing.append("metric_value_numeric")

    if requirements.get("timelapse_required"):
        frame_values = [record.get("timelapse_frame") for record in records if isinstance(record, dict)]
        if not any(str(value or "").strip() for value in frame_values):
            blocking_missing.append("timelapse_frame")

    latitude_values = [record.get("latitude") for record in records if isinstance(record, dict)]
    longitude_values = [record.get("longitude") for record in records if isinstance(record, dict)]
    latitude_ready = pd.to_numeric(pd.Series(latitude_values), errors="coerce").notna().any() if latitude_values else False
    longitude_ready = pd.to_numeric(pd.Series(longitude_values), errors="coerce").notna().any() if longitude_values else False

    if records and (not latitude_ready or not longitude_ready):
        missing_for_direct_plotting.extend(
            field for field, ready in [("latitude", latitude_ready), ("longitude", longitude_ready)] if not ready
        )
        if non_empty_geo_values and "geo_label_non_numeric" not in blocking_missing:
            needs_geo_enrichment = True
            warnings.append("Latitude/longitude are missing, but geo_label is available for Module 3 geo-enrichment.")
        else:
            blocking_missing.extend(missing_for_direct_plotting)

    ready_for_module_3 = bool(records) and not blocking_missing
    is_map_ready = ready_for_module_3 and not needs_geo_enrichment and not missing_for_direct_plotting

    map_readiness = {
        "is_map_ready": is_map_ready,
        "ready_for_module_3": ready_for_module_3,
        "needs_geo_enrichment": needs_geo_enrichment,
        "missing_for_direct_plotting": sorted(set(missing_for_direct_plotting)),
        "blocking_missing_fields": sorted(set(blocking_missing)),
        "plotting_level": mapped_fields.get("geo_field") or "geo_label",
        "intensity_field": "metric_value" if numeric_metric_values.notna().sum() > 0 else None,
        "time_field": "timelapse_frame" if requirements.get("timelapse_required") else "time_period",
        "records_count": len(records),
        "warnings": warnings,
    }

    logger.add(
        "Validate Map Readiness",
        "success" if ready_for_module_3 else "warning",
        [
            "Checked visualization records for geo_label, metric_value, optional coordinates, and timelapse_frame.",
            "Marked output ready for Module 3 only when required plotting fields are available.",
        ],
        map_readiness,
    )

    return map_readiness


# ============================================================
# STEP SUMMARY LLM
# ============================================================

def refine_step_summary_with_llm(step_log: List[Dict[str, Any]], inputs_considered: Dict[str, bool]) -> List[Dict[str, Any]]:
    if not USE_LLM_FOR_STEP_SUMMARY:
        return [
            {
                "step_name": step.get("step_name"),
                "status": step.get("status"),
                "bullet_points": step.get("changes_made", []),
            }
            for step in step_log
        ]

    system = """
You are a technical documentation assistant.

Convert the step log into clean bullet-point summaries.
Return only valid JSON:
{
  "step_summaries": [
    {
      "step_name": "",
      "status": "",
      "bullet_points": []
    }
  ]
}
"""

    user = {
        "step_log": step_log,
        "inputs_considered": inputs_considered,
    }

    fallback = {
        "step_summaries": [
            {
                "step_name": step.get("step_name"),
                "status": step.get("status"),
                "bullet_points": step.get("changes_made", []),
            }
            for step in step_log
        ]
    }

    result = call_llm_json(
        call_name="Step Summary LLM",
        system_prompt=system,
        user_prompt=safe_json_dumps(user),
        fallback=fallback,
    )

    return result.get("step_summaries", fallback["step_summaries"])


# ============================================================
# MAIN MODULE 2 RUNNER
# ============================================================

def run_module_2(
    shortlisted_df: pd.DataFrame,
    mapping_schema: Dict[str, Any],
    module_1_intent_json: Dict[str, Any],
    retrieval_context: Optional[Dict[str, Any]] = None,
    retrieval_sql_query: str = "",
    inputs_considered: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    logger = StepLogger()
    start_time = time.time()

    default_inputs_considered = {
        "retrieved_data": True,
        "data_mapping": True,
        "module_1_intent": True,
        "retrieval_model_intent": True,
        "retrieval_sql_query": False,
    }
    if inputs_considered:
        default_inputs_considered.update(inputs_considered)
    inputs_considered = default_inputs_considered
    inputs_considered["retrieved_data"] = True

    retrieval_context = retrieval_context or {}
    column_mapping = mapping_schema.get("column_mapping", {})

    df = shortlisted_df.copy()
    df.columns = [normalize_column_name(c) for c in df.columns]

    logger.add(
        "Receive and Normalize Inputs",
        "success",
        [
            "Received shortlisted data from Data Retrieval Agent sample Excel file.",
            "Converted data into Pandas DataFrame.",
            "Cleaned column names by removing extra spaces.",
            f"Source type set to {SOURCE_TYPE}.",
            f"row_limit_applied set to {ROW_LIMIT_APPLIED}.",
            f"Inputs considered: {inputs_considered}.",
        ],
        {
            "rows_received": len(df),
            "columns_received": list(df.columns),
            "mapping_type": mapping_schema.get("mapping_type"),
            "retrieval_context_available": bool(retrieval_context),
            "retrieval_sql_query_available": bool(retrieval_sql_query),
            "inputs_considered": inputs_considered,
        },
    )

    requirements = extract_module_1_requirements(module_1_intent_json)

    logger.add(
        "Read Module 1 Intent",
        "success" if inputs_considered.get("module_1_intent") else "skipped",
        [
            "Extracted user query, business objective, metric, locations, time range, map type, and timelapse requirement."
            if inputs_considered.get("module_1_intent")
            else "Module 1 intent was not considered. Default minimal intent was used.",
            "Read data grain from Module 1 intent where available.",
            "Read map_output_requirements for visualization-specific preparation.",
        ],
        requirements,
    )

    logger.add(
        "Read Retrieval Context",
        "success" if inputs_considered.get("retrieval_model_intent") and retrieval_context else "skipped",
        [
            "Read retrieval context explaining what, why, and how data was retrieved."
            if inputs_considered.get("retrieval_model_intent") and retrieval_context
            else "Retrieval model intent/context was not considered.",
        ],
        retrieval_context if inputs_considered.get("retrieval_model_intent") else {},
    )

    logger.add(
        "Read Retrieval SQL Query",
        "success" if inputs_considered.get("retrieval_sql_query") and retrieval_sql_query else "skipped",
        [
            "Read SQL query generated by Data Retrieval Model."
            if inputs_considered.get("retrieval_sql_query") and retrieval_sql_query
            else "Retrieval SQL query was not considered.",
        ],
        {"sql_query": retrieval_sql_query[:4000]} if inputs_considered.get("retrieval_sql_query") else {},
    )

    det_mapping = deterministic_field_mapping(df, column_mapping)

    llm_mapping = llm_field_mapping(
        df=df,
        mapping_schema=mapping_schema,
        requirements=requirements,
        deterministic_mapping=det_mapping,
        retrieval_context=retrieval_context,
        retrieval_sql_query=retrieval_sql_query,
        inputs_considered=inputs_considered,
    )

    merged_mapping = merge_field_mappings(det_mapping, llm_mapping, df, column_mapping)
    mapped_fields = merged_mapping["mapped_fields"]
    repaired_mapped_fields, time_repair = repair_time_field_mapping(df, mapped_fields, requirements)
    if time_repair.get("changed"):
        mapped_fields = repaired_mapped_fields
        merged_mapping["mapped_fields"] = mapped_fields
        merged_mapping.setdefault("mapping_source", {})["time_field"] = "time_granularity_repair"
        logger.add(
            "Repair Time Field Mapping",
            "success",
            [
                "Adjusted mapped time field to match Module 1 time granularity.",
                "Prevents annual intent from using quarter labels as the primary time field.",
            ],
            time_repair,
        )

    logger.add(
        "Create Standard Field Mapping",
        "success",
        [
            "Mapped actual dataset columns to standard internal fields.",
            "Used deterministic mapping and LLM-assisted mapping where available.",
            "Merged mapping results using confidence and actual column availability.",
            "Applied geo_field safety correction to prevent numeric metric columns from becoming geo_label.",
        ],
        merged_mapping,
    )

    missing_fields = required_fields_for_intent(requirements, mapped_fields)

    if missing_fields:
        return create_missing_fields_output(
            missing_fields,
            df,
            mapping_schema,
            requirements,
            logger,
            inputs_considered,
        )

    logger.add(
        "Validate Required Fields",
        "success",
        [
            "Minimum required fields are available for first-iteration processing.",
            "No blocking missing fields found before filtering and aggregation.",
        ],
        {"missing_fields": missing_fields},
    )

    filtered_df, filter_info = apply_missing_filters(df, requirements, mapped_fields, retrieval_context, logger)
    timed_df, time_info = create_time_fields(filtered_df, requirements, mapped_fields, logger)
    time_filter_info = ensure_dict(time_info.get("filter_validation"))
    if time_filter_info:
        filter_info.setdefault("filters_applied_by_module_2", {}).update(
            ensure_dict(time_filter_info.get("filters_applied_by_module_2"))
        )
        filter_info.setdefault("filters_validated", {}).update(
            ensure_dict(time_filter_info.get("filters_validated"))
        )
        filter_info.setdefault("warnings", []).extend(ensure_list(time_filter_info.get("warnings")))

    analysis_df, agg_summary_or_error = calculate_metric_and_aggregate(
        timed_df,
        requirements,
        mapped_fields,
        retrieval_context,
        mapping_schema,
        retrieval_sql_query,
        inputs_considered,
        logger,
    )

    if analysis_df is None:
        missing_explanation = llm_missing_explainer(
            [
                {
                    "standard_field": "metric_logic",
                    "required_for": ["aggregation", "map intensity", "analysis_ready_dataset"],
                    "reason": agg_summary_or_error.get("reason", "Metric could not be calculated."),
                }
            ],
            timed_df,
            mapping_schema,
            requirements,
            inputs_considered,
        )

        logger.add(
            "Generate Missing Metric Logic JSON",
            "missing_metric_logic",
            [
                "Metric could not be calculated from available data.",
                "Created structured missing metric logic output.",
            ],
            missing_explanation,
        )

        return {
            "module_number": 2,
            "module_name": "Data Restructuring & Filtering",
            "status": "missing_metric_logic",
            "next_module_ready": False,
            "inputs_considered": inputs_considered,
            "missing_metric_logic": missing_explanation,
            "available_columns": list(df.columns),
            "mapped_fields": mapped_fields,
            "data_quality_summary": {
                "rows_received": len(df),
                "rows_after_filtering": len(timed_df),
                "warnings": ["Required metric could not be calculated."],
            },
            "debug_metadata": {
                "step_log": logger.to_list(),
                "llm_token_ledger": get_token_ledger_summary(),
            },
        }

    visualization_output = build_visualization_ready_output(analysis_df, requirements, inputs_considered, logger)
    map_readiness = validate_map_readiness(visualization_output, requirements, mapped_fields, logger)
    next_module_ready = bool(map_readiness.get("ready_for_module_3"))
    output_status = "success" if next_module_ready else "map_not_ready"

    warnings = []
    if ROW_LIMIT_APPLIED:
        warnings.append("row_limit_applied=true. Results are based on limited rows.")

    if not mapped_fields.get("latitude_field") or not mapped_fields.get("longitude_field"):
        warnings.append("Latitude/longitude fields are missing or unmapped. Module 3 may need geo-enrichment.")

    data_quality_summary = {
        "rows_received": len(df),
        "rows_after_filtering": len(timed_df),
        "rows_after_aggregation": len(analysis_df),
        "columns_received": list(df.columns),
        "missing_required_fields": [],
        "warnings": warnings
        + filter_info.get("warnings", [])
        + time_info.get("warnings", [])
        + ensure_list(map_readiness.get("warnings")),
        "filtered_raw_dataset_sample": timed_df.head(10).to_dict(orient="records"),
    }

    logger.add(
        "Generate Debug and Quality Metadata",
        "success",
        [
            "Generated mapped fields summary.",
            "Generated filter validation summary.",
            "Generated data quality summary.",
            "Included filtered raw dataset sample for development debugging.",
        ],
        data_quality_summary,
    )

    total_seconds = round(time.time() - start_time, 3)

    module_2_output = {
        "module_number": 2,
        "module_name": "Data Restructuring & Filtering",
        "status": output_status,
        "next_module_ready": next_module_ready,
        "source_type": SOURCE_TYPE,
        "row_limit_applied": ROW_LIMIT_APPLIED,
        "inputs_considered": inputs_considered,
        "processing_time_seconds": total_seconds,
        "input_summary": {
            "rows_received": len(df),
            "columns_received": list(df.columns),
            "data_grain": requirements.get("data_grain"),
            "retrieval_context_available": bool(retrieval_context),
            "retrieval_sql_query_available": bool(retrieval_sql_query),
            "data_mapping_type": mapping_schema.get("mapping_type"),
        },
        "mapped_fields": mapped_fields,
        "filter_validation": filter_info,
        "aggregation_summary": agg_summary_or_error,
        "analysis_ready_dataset": analysis_df.to_dict(orient="records"),
        "visualization_ready_output": visualization_output,
        "map_readiness": map_readiness,
        "data_quality_summary": data_quality_summary,
        "debug_metadata": {
            "step_log": logger.to_list(),
            "step_summaries": [],
            "module_1_requirements_used": requirements,
            "map_readiness": map_readiness,
            "llm_token_ledger": get_token_ledger_summary(),
        },
    }

    logger.add(
        "Return Final Module 2 Output",
        "success",
        [
            "Returned analysis_ready_dataset.",
            "Returned visualization_ready_output.",
            "Returned map_readiness validation.",
            "Returned debug metadata and step-by-step processing log.",
            f"Set next_module_ready={next_module_ready}.",
        ],
        {"processing_time_seconds": total_seconds},
    )

    module_2_output["debug_metadata"]["step_log"] = logger.to_list()
    module_2_output["debug_metadata"]["step_summaries"] = refine_step_summary_with_llm(
        logger.to_list(),
        inputs_considered,
    )
    module_2_output["debug_metadata"]["llm_token_ledger"] = get_token_ledger_summary()

    return module_2_output


# ============================================================
# HIGH-LEVEL ENTRY POINT (called by API route)
# ============================================================

def run_module_2_from_paths(
    inputs_considered: Dict[str, bool],
    retrieved_data_path: str = "",
    data_mapping_path: str = "",
    module_1_intent_path: str = "",
    module_1_intent_json: Optional[Dict[str, Any]] = None,
    retrieval_context_path: str = "",
    retrieval_sql_path: str = "",
) -> Dict[str, Any]:
    """Load files from disk and delegate to run_module_2.

    This is the function the FastAPI route calls.
    """
    global _active_ledger
    _active_ledger = TokenLedger()

    try:
        shortlisted_df = load_excel_data(retrieved_data_path or DEFAULT_RETRIEVED_DATA_PATH)

        if inputs_considered.get("data_mapping", True):
            mapping_schema = load_data_mapping_schema(data_mapping_path or DEFAULT_DATA_MAPPING_PATH)
        else:
            mapping_schema = empty_mapping_schema()

        if inputs_considered.get("module_1_intent", True) and module_1_intent_json:
            module_1_intent = module_1_intent_json
        elif inputs_considered.get("module_1_intent", True):
            module_1_intent = load_json_file(module_1_intent_path or DEFAULT_MODULE_1_INTENT_PATH)
        else:
            module_1_intent = default_module_1_intent()

        retrieval_context: Dict[str, Any] = {}
        if inputs_considered.get("retrieval_model_intent", False):
            rc_path = retrieval_context_path or DEFAULT_RETRIEVAL_CONTEXT_PATH
            if Path(rc_path).exists():
                retrieval_context = load_json_file(rc_path)

        retrieval_sql_query = ""
        if inputs_considered.get("retrieval_sql_query", False):
            sql_path = retrieval_sql_path or DEFAULT_RETRIEVAL_SQL_PATH
            if Path(sql_path).exists():
                retrieval_sql_query = load_text_file(sql_path)

        output = run_module_2(
            shortlisted_df=shortlisted_df,
            mapping_schema=mapping_schema,
            module_1_intent_json=module_1_intent,
            retrieval_context=retrieval_context,
            retrieval_sql_query=retrieval_sql_query,
            inputs_considered=inputs_considered,
        )
        return output
    finally:
        _active_ledger = None

