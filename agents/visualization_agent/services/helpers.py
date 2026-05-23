"""
Visualization Agent Module 1 — JSON handling, type coercion, and block normalization helpers.
"""

import json
import re
from typing import Any, Dict, List

from .constants import MODULE_NAMES


# ============================================================
# JSON HANDLING
# ============================================================

def extract_json_from_text(text: str) -> Dict[str, Any]:
    """Extracts valid JSON from plain text or fenced JSON response."""
    if not text:
        raise ValueError("Empty model response.")

    cleaned = text.strip()
    fenced_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced_match:
        cleaned = fenced_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace == -1 or last_brace == -1:
        raise ValueError("No JSON object found in model response.")

    json_text = cleaned[first_brace : last_brace + 1]
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON returned by model: {exc}") from exc


def safe_json_dumps(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


# ============================================================
# TYPE COERCION
# ============================================================

def ensure_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def ensure_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


# ============================================================
# BLOCK NORMALIZATION
# ============================================================

def normalize_active_flag(block: Dict[str, Any], default: bool = False) -> Dict[str, Any]:
    block = ensure_dict(block)
    block["is_active"] = bool(block.get("is_active", default))
    block.setdefault("additional_parameters", {})
    return block


def ensure_block_defaults(output: Dict[str, Any], user_query: str = "") -> Dict[str, Any]:
    """
    Hybrid schema rule:
    - Fixed outer structure is always present.
    - Inner blocks remain flexible and allow dynamic fields.
    - Each inner block gets additional_parameters for future/dynamic query fields.
    """
    output = ensure_dict(output)

    output.setdefault("module_number", 1)
    output.setdefault("module_name", MODULE_NAMES["module_1"])
    output.setdefault(
        "module_purpose",
        "Understand the user query and decide what the Visualization Agent must do.",
    )
    output.setdefault("user_query", user_query)
    output.setdefault("business_objective", "")

    output["structured_intent"] = ensure_dict(output.get("structured_intent"))
    output["request_classification"] = ensure_dict(output.get("request_classification"))
    output["execution_flags"] = ensure_dict(output.get("execution_flags"))
    output["active_requirement_blocks"] = ensure_list(output.get("active_requirement_blocks"))
    output["required_modules"] = ensure_list(output.get("required_modules"))
    output["execution_plan"] = ensure_list(output.get("execution_plan"))
    output["validation_status"] = ensure_dict(output.get("validation_status"))

    output["map_output_requirements"] = normalize_active_flag(
        output.get("map_output_requirements"), default=True
    )
    output["simulation_requirements"] = normalize_active_flag(
        output.get("simulation_requirements"), default=False
    )
    output["what_if_requirements"] = normalize_active_flag(
        output.get("what_if_requirements"), default=False
    )
    output["spatial_requirements"] = normalize_active_flag(
        output.get("spatial_requirements"), default=False
    )
    output["insight_requirements"] = normalize_active_flag(
        output.get("insight_requirements"), default=True
    )
    output["intent_mapping"] = ensure_dict(output.get("intent_mapping"))

    return output
