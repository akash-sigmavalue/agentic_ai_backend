from __future__ import annotations

import json
import re

from openai import OpenAI


SPACE_METADATA_LINE_PATTERN = re.compile(
    r"^(?:selected_options|user_does_not_have_more_space_details|other_text|additional_details)\s*=\s*.*$",
    re.IGNORECASE | re.MULTILINE,
)

CLARIFICATION_ANSWER_PATTERN = re.compile(
    r"User answer:\n(.*?)(?:\n\nClarification round|\Z)",
    re.IGNORECASE | re.DOTALL,
)

SPACE_FIELD_PATTERNS = {
    "unit_number": [
        r"\bunit\s*(?:no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9/\- ]{0,60})",
        r"\bflat\s*(?:no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9/\- ]{0,60})",
        r"\bapartment\s*(?:no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9/\- ]{0,60})",
        r"\bapt\s*(?:no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9/\- ]{0,60})",
    ],
    "tower_name": [
        r"\btower\s*(?:name|no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bbuilding\s*(?:name|no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bblock\s*(?:name|no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bwing\s*(?:name|no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
    ],
    "plot_number": [
        r"\b(?:plot|survey|cts|khasra|parcel)\s*(?:no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9/\- ]{0,60})",
    ],
    "project_name": [
        r"\bproject\s*(?:name|is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bsociety\s*(?:name|is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bcomplex\s*(?:name|is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bestate\s*(?:name|is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bresidency\s*(?:name|is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
    ],
    "location_name": [
        r"\blocation\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\barea\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\blocality\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bvillage\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
    ],
    "micro_market": [
        r"\bmicro\s*market\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
        r"\bmicromarket\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
    ],
    "city": [
        r"\bcity\s*(?:is|:|-)\s*([A-Za-z][A-Za-z\s\-]{1,60})",
    ],
    "state_name": [
        r"\bstate\s*(?:is|:|-)\s*([A-Za-z][A-Za-z\s\-]{1,60})",
    ],
    "country_name": [
        r"\bcountry\s*(?:is|:|-)\s*([A-Za-z][A-Za-z\s\-]{1,60})",
    ],
    "sub_locality": [
        r"\bsub\s*locality\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
    ],
    "pincode": [
        r"\b(\d{6})\b",
    ],
    "village_name": [
        r"\bvillage\s*(?:is|:|-)\s*([A-Za-z0-9][A-Za-z0-9&/\-\. ]{0,80})",
    ],
}

LATITUDE_PATTERN = re.compile(
    r"\b(?:lat|latitude|latitute)\s*(?:is|=|:|-)?\s*(-?\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

LONGITUDE_PATTERN = re.compile(
    r"\b(?:lon|long|lng|longitude)\s*(?:is|=|:|-)?\s*(-?\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

RADIUS_PATTERN = re.compile(
    r"\b(?:(?:within|under|inside)\s*)?(\d+(?:\.\d+)?)\s*(?:km|kilometer|kilometers|kms)\s*(?:radius)?\b|"
    r"\bradius\s*(?:of|is|=|:|-)?\s*(\d+(?:\.\d+)?)\s*(?:km|kilometer|kilometers|kms)\b",
    re.IGNORECASE,
)


def _clean_space_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip(" ,.;:-"))
    cleaned = re.sub(r"(?i)\b(?:is|are|was|were|please|details?|name)\b\s*$", "", cleaned).strip(" ,.;:-")
    return cleaned


def _extract_clarification_answer_text(text: str) -> str:
    if not text:
        return ""
    matches = CLARIFICATION_ANSWER_PATTERN.findall(text)
    if matches:
        return matches[-1].strip()
    return text


def extract_space_filters(text: str, field_order: tuple[str, ...]) -> tuple[dict, str]:
    candidate = _extract_clarification_answer_text(text)
    normalized_lines = []
    for raw_line in candidate.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("selected_options="):
            continue
        if lower.startswith("user_does_not_have_more_space_details="):
            continue
        if lower.startswith("other_text="):
            normalized_lines.append(line.split("=", 1)[1].strip())
            continue
        if lower.startswith("additional_details="):
            normalized_lines.append(line.split("=", 1)[1].strip())
            continue
        if lower in {"space clarification response:", "user answer:"}:
            continue
        if lower.startswith("clarification round"):
            continue
        normalized_lines.append(line)
    candidate = " ".join(normalized_lines).strip()

    filters: dict[str, str] = {}
    primary_field = ""

    for field in field_order:
        patterns = SPACE_FIELD_PATTERNS.get(field, [])
        for pattern in patterns:
            match = re.search(pattern, candidate, flags=re.IGNORECASE)
            if not match:
                continue
            extracted = _clean_space_value(match.group(1) if match.groups() else match.group(0))
            if not extracted:
                continue
            filters[field] = extracted
            if not primary_field:
                primary_field = field
            break

    return filters, primary_field


def extract_coordinate_radius_filters(text: str) -> dict[str, float]:
    """
    Extract coordinate-based spatial context from a user query.

    This is intentionally separate from space_filters because latitude,
    longitude, and radius are not text geography filters; together they define
    a concrete search area.
    """
    candidate = _extract_clarification_answer_text(text or "")
    lat_match = LATITUDE_PATTERN.search(candidate)
    lon_match = LONGITUDE_PATTERN.search(candidate)
    if not lat_match or not lon_match:
        return {}

    try:
        latitude = float(lat_match.group(1))
        longitude = float(lon_match.group(1))
    except ValueError:
        return {}

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {}

    filters: dict[str, float] = {
        "latitude": latitude,
        "longitude": longitude,
    }

    radius_match = RADIUS_PATTERN.search(candidate)
    if radius_match:
        raw_radius = radius_match.group(1) or radius_match.group(2)
        try:
            radius_km = float(raw_radius)
        except (TypeError, ValueError):
            radius_km = 0
        if radius_km > 0:
            filters["radius_km"] = radius_km

    return filters


SPACE_INFERENCE_PROMPT = """
You are a real-estate space extractor.
Your job is to identify which space the user is referring to, not to repeat the full sentence.

Important:
- Extract the space entity only.
- Do not copy action words like show, list, launched, price, trend, booked, sold, from, in, between, after, before.
- If the user asks about projects launched in a location, the location is the space.
- If the user asks about a specific project/society/building name, use project_name.
- If the query only mentions the activity and no clear space, ask for clarification.

Examples:
- "Show projects launched in 2023 from Baner" -> location_name = Baner, project_name = empty
- "Price trend for Lodha Park" -> project_name = Lodha Park
- "Transactions in Andheri West" -> location_name = Andheri West
- "Show projects launched in 2023" -> needs clarification

Return JSON only:
{{
  "needs_clarification": false,
  "clarification_question": "",
  "analysis_level": "",
  "filters": {{}},
  "clear_fields": []
}}

Current filters:
{current_filters}

User query:
{user_query}
"""


def _safe_json_loads(payload: str) -> dict:
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def infer_space_context(
    client: OpenAI | None,
    user_query: str,
    current_filters: dict | None = None,
) -> dict:
    if not client or not (user_query or "").strip():
        return {
            "needs_clarification": False,
            "clarification_question": "",
            "analysis_level": "",
            "filters": {},
            "clear_fields": [],
        }

    prompt = SPACE_INFERENCE_PROMPT.format(
        current_filters=json.dumps(current_filters or {}, indent=2),
        user_query=user_query,
    )
    try:
        response = client.chat.completions.create(
            model="gpt-5.1",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
            timeout=20,
        )
        parsed = _safe_json_loads(response.choices[0].message.content)
    except Exception:
        return {
            "needs_clarification": False,
            "clarification_question": "",
            "analysis_level": "",
            "filters": {},
            "clear_fields": [],
        }

    filters = parsed.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    clear_fields = parsed.get("clear_fields") or []
    if not isinstance(clear_fields, list):
        clear_fields = []

    return {
        "needs_clarification": bool(parsed.get("needs_clarification")),
        "clarification_question": str(parsed.get("clarification_question") or "").strip(),
        "analysis_level": str(parsed.get("analysis_level") or "").strip(),
        "filters": {k: v for k, v in filters.items() if isinstance(k, str) and v not in (None, "")},
        "clear_fields": [field for field in clear_fields if isinstance(field, str) and field],
    }
