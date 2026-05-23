"""
Visualization Agent Module 1 — Time detection and time-requirement repair logic.
"""

import json
import re
from typing import Any, Dict, Tuple

from .helpers import ensure_dict, ensure_list


def _flatten_text(value: Any) -> str:
    """Converts nested intent/config values into lower-case text for rule checks."""
    try:
        return json.dumps(value, ensure_ascii=False).lower()
    except TypeError:
        return str(value).lower()


def _month_or_quarter_mentioned(text: str) -> Tuple[bool, bool]:
    """Return (month_mentioned, quarter_mentioned) using conservative patterns."""
    quarter_mentioned = bool(
        re.search(r"\b(q[1-4]|quarter|quarterly)\b", text, flags=re.IGNORECASE)
    )

    month_names = [
        "january", "february", "march", "april", "june", "july", "august",
        "september", "october", "november", "december"
    ]
    month_abbr = ["jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"]

    month_mentioned = bool(
        re.search(r"\b(month|monthly)\b", text, flags=re.IGNORECASE)
        or re.search(r"\b(" + "|".join(month_names + month_abbr) + r")\b", text, flags=re.IGNORECASE)
        or re.search(r"\bmay\s+\d{4}\b", text, flags=re.IGNORECASE)
        or re.search(r"\b\d{1,2}\s+may\b", text, flags=re.IGNORECASE)
    )
    return month_mentioned, quarter_mentioned


def _normalize_time_granularity(value: Any) -> str:
    """Normalize LLM/user granularity values into stable downstream labels."""
    text = str(value or "").strip().lower()
    if text in ["year", "years", "annual", "annually", "yearly"]:
        return "annual"
    if text in ["quarter", "quarters", "quarterly"]:
        return "quarterly"
    if text in ["month", "months", "monthly"]:
        return "monthly"
    if text in ["date", "day", "daily"]:
        return "date"
    return text


def detect_time_requirement(output: Dict[str, Any], user_query: str) -> Tuple[bool, str, bool, str]:
    """
    Detects whether the user query/intent needs a time field and whether the map
    should be time-aware/timelapse-ready.

    Return tuple:
        (time_field_required, time_granularity, timelapse_required, reason)
    """
    intent = ensure_dict(output.get("structured_intent"))
    map_req = ensure_dict(output.get("map_output_requirements"))
    user_text = str(user_query or "").lower()
    all_text = f"{user_text} {_flatten_text(intent)} {_flatten_text(map_req)}".lower()

    year_matches = re.findall(r"\b(?:19|20)\d{2}\b", all_text)
    unique_years = sorted(set(year_matches))

    has_year_range_pattern = bool(
        re.search(r"\b(?:19|20)\d{2}\s*(?:to|through|till|until|and|[-–—])\s*(?:19|20)\d{2}\b", all_text)
        or re.search(r"\b(from|between)\b.*\b(to|and|through|till|until)\b", all_text)
    )

    possible_time_blocks = [
        intent.get("time_range"),
        intent.get("time_period"),
        intent.get("date_range"),
        intent.get("period"),
        map_req.get("time_range"),
        ensure_dict(map_req.get("additional_parameters")).get("time_range"),
        ensure_dict(map_req.get("additional_parameters")).get("time_period"),
    ]

    has_structured_range = False
    structured_granularity = ""
    for block in possible_time_blocks:
        if isinstance(block, dict) and block:
            keys = {str(k).lower() for k in block.keys()}
            if any(k in keys for k in ["start", "end", "start_year", "end_year", "start_date", "end_date", "from", "to"]):
                has_structured_range = True
            if block.get("granularity"):
                structured_granularity = _normalize_time_granularity(block.get("granularity"))
        elif isinstance(block, list) and block:
            has_structured_range = True

    time_dimension = _normalize_time_granularity(
        intent.get("time_dimension") or intent.get("temporal_dimension") or ""
    )

    month_mentioned, quarter_mentioned = _month_or_quarter_mentioned(user_text)
    if not (month_mentioned or quarter_mentioned):
        month_mentioned, quarter_mentioned = _month_or_quarter_mentioned(all_text.replace('"may"', ''))

    if quarter_mentioned:
        granularity = "quarterly"
    elif month_mentioned:
        granularity = "monthly"
    elif len(unique_years) >= 1 or has_year_range_pattern:
        granularity = "annual"
    elif structured_granularity:
        granularity = structured_granularity
    elif time_dimension:
        granularity = time_dimension
    elif any(term in all_text for term in ["trend", "growth", "change over time", "timelapse", "time lapse", "yoy", "year-over-year"]):
        granularity = "auto"
    else:
        granularity = ""

    time_field_required = bool(
        unique_years
        or has_structured_range
        or time_dimension
        or re.search(r"\b(year|yearly|annual|quarter|quarterly|month|monthly|trend|growth|yoy|year-over-year|timelapse|time lapse|period)\b", all_text)
    )

    multi_period_range = bool(
        len(unique_years) >= 2
        or has_year_range_pattern
        or has_structured_range
        or re.search(r"\b(trend|growth|yoy|year-over-year|timelapse|time lapse|over time)\b", all_text)
    )

    timelapse_required = bool(time_field_required and multi_period_range)

    if timelapse_required:
        reason = "A multi-period time range or trend was detected, so the map must be time-aware with a time-slider/timelapse payload."
    elif time_field_required:
        reason = "A time field is required for filtering/grouping, but timelapse is not mandatory because only a single period was detected."
    else:
        reason = "No time range or temporal analysis requirement was detected."

    return time_field_required, granularity or None, timelapse_required, reason


def apply_time_requirements_to_map(output: Dict[str, Any], user_query: str) -> Dict[str, Any]:
    """Repairs map_output_requirements so time ranges always become time-aware map requirements."""
    map_req = ensure_dict(output.get("map_output_requirements"))
    additional_parameters = ensure_dict(map_req.get("additional_parameters"))
    layer_requirements = ensure_dict(map_req.get("layer_requirements"))

    time_required, granularity, timelapse_required, reason = detect_time_requirement(output, user_query)

    if time_required:
        map_req["time_field_required"] = True
        if not map_req.get("time_granularity"):
            map_req["time_granularity"] = granularity

        additional_parameters.setdefault("time_requirement_reason", reason)
        additional_parameters.setdefault("time_aware_map", True)

        if timelapse_required:
            map_req["timelapse_required"] = True
            map_req.setdefault("timelapse_mode", "time_slider")
            additional_parameters.setdefault("timelapse_payload_required", True)
            additional_parameters.setdefault("timelapse_mode", map_req.get("timelapse_mode", "time_slider"))
            additional_parameters.setdefault(
                "timelapse_rule",
                "Keep the primary map type unchanged and add a time-slider/timelapse payload for multi-period time ranges.",
            )
            layer_requirements["needs_timelapse_layer"] = True
        else:
            map_req.setdefault("timelapse_required", False)
            additional_parameters.setdefault("timelapse_payload_required", False)
    else:
        map_req.setdefault("time_field_required", False)
        map_req.setdefault("time_granularity", None)
        map_req.setdefault("timelapse_required", False)
        additional_parameters.setdefault("time_aware_map", False)

    map_req["additional_parameters"] = additional_parameters
    map_req["layer_requirements"] = layer_requirements
    output["map_output_requirements"] = map_req
    return output
