from __future__ import annotations

"""
Comparable Search Tool
Provides two comparable-search strategies:

1. comparable_selection_agent()  — LLM web-search (GPT-4o, iterative radius expansion)
2. fetch_db_comparables()        — Internal DB via /ask_stream_data_retrieval SSE

Confidence scoring (pure Python, no LLM):
  - Distance      : 40% weight  → purely numeric km-based
  - Location      : 40% weight  → purely text/name-based (NO distance used here)
  - Property Type : 20% weight  → uses property_type field

LOCATION SCORING PHILOSOPHY:
  Location and Distance are TWO SEPARATE factors.
  _score_location() NEVER looks at km — it only compares location name strings.
  Examples:
    "Mundhwa"          vs "Mundhwa"            → 100 (exact)
    "Mundhwa, Pune"    vs "Mundhwa"            → 100 (exact after norm)
    "Mundhwa East"     vs "Mundhwa"            → 85  (first token + shared)
    "Kharadi"          vs "Mundhwa, Pune"      → 25  (same city "Pune")
    "Baner"            vs "Mundhwa"            → 0   (no relation)
"""

import json
import math
import re
import logging
import os
import requests
import sys
import time

from openai import OpenAI
from dotenv import load_dotenv
from tools.valuation.map_search import search_coordinates

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# ── Confidence Scoring ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _score_location(comp_location: str, subject_location: str) -> int:
    """
    Pure text/name-based location match. NO distance (km) used here at all.
    Distance has its own separate 40% weight via _score_distance().

    Simple 3-tier logic:
      100 → Same locality family
            (Mundhwa = Mundhwa, Mundhwa East = Mundhwa, Phase 1 = Phase 2, etc.)
            Rule: first meaningful locality token matches between both strings.

       50 → Different locality but same city
            (Kharadi vs Mundhwa — both in Pune)

        0 → Completely different / no relation
    """
    if not comp_location or not subject_location:
        return 0

    # Words that are NOT locality names — strip these before comparing
    STRIP_WORDS = {
        "the", "a", "an", "of", "in", "at", "near", "and", "by",
        "east", "west", "north", "south", "new", "old",
        "sector", "phase", "block", "tower", "wing", "road", "rd",
        "street", "st", "avenue", "ave", "lane", "marg", "extension",
        "part", "plot", "society", "colony", "nagar", "vihar",
    }

    CITY_WORDS = {
        "pune", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai",
        "delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad",
        "dubai", "abudhabi", "sharjah", "singapore", "london", "toronto",
        "thane", "navi", "pimpri", "chinchwad", "nashik", "nagpur",
    }

    def locality_tokens(s: str) -> list[str]:
        """Extract meaningful locality name words, drop qualifiers & city names."""
        s = s.lower().strip()
        s = re.sub(r'[,\-/\\]+', ' ', s)
        s = re.sub(r'\s+', ' ', s)
        return [
            w for w in s.split()
            if len(w) > 2
            and w not in STRIP_WORDS
            and w not in CITY_WORDS
        ]

    def city_tokens(s: str) -> set[str]:
        s = s.lower()
        return {w for w in re.split(r'[\s,\-/\\]+', s) if w in CITY_WORDS}

    c_loc = locality_tokens(comp_location)
    s_loc = locality_tokens(subject_location)

    # ── 100: Same locality family ─────────────────────────────────────────
    # If the first meaningful locality word is the same, they belong to the
    # same micro-market regardless of qualifiers (East/West/Phase 1/Phase 2).
    # e.g. "Mundhwa"       vs "Mundhwa East"   → both start with "mundhwa" → 100
    # e.g. "Keshav Nagar Phase 1" vs "Keshav Nagar Phase 2" → "keshav" matches → 100
    # e.g. "Baner"         vs "Baner Road"     → "baner" matches → 100
    if c_loc and s_loc and c_loc[0] == s_loc[0]:
        return 100

    # Also check if ANY locality token from one fully appears in the other
    # handles "Kalyani Nagar" vs "Nagar, Pune" edge cases
    if c_loc and s_loc and (set(c_loc) & set(s_loc)):
        return 100

    # ── 50: Same city, different locality ────────────────────────────────
    c_cities = city_tokens(comp_location)
    s_cities = city_tokens(subject_location)
    if c_cities and s_cities and (c_cities & s_cities):
        return 50

    # ── 0: Completely different ───────────────────────────────────────────
    return 0


def _score_distance(distance_km: float | None) -> int:
    """
    Purely numeric proximity score. Completely separate from location name scoring.

    100 → ≤ 0.5 km
     92 → 0.5 – 1 km
     82 → 1 – 2 km
     70 → 2 – 3 km
     50 → 3 – 5 km
     22 → 5 – 10 km
      0 → > 10 km or unknown
    """
    if distance_km is None:
        return 0
    try:
        d = float(distance_km)
    except (TypeError, ValueError):
        return 0

    if d <= 0.5:  return 100
    if d <= 1.0:  return 92
    if d <= 2.0:  return 82
    if d <= 3.0:  return 70
    if d <= 5.0:  return 50
    if d <= 10.0: return 22
    return 0


def _score_property_type(comp_type: str, subject_type: str) -> int:
    """
    Scores based on property_type field (apartment / villa / plot / office / retail).
    NOT project_category (Residential / Commercial).

    100 → exact same type
     85 → near-identical  (apartment ↔ flat)
     70 → closely related (villa ↔ independent house, plot ↔ villa)
     40 → same broad class (office ↔ commercial_office, shop ↔ retail)
      0 → unrelated
    """
    if not comp_type or not subject_type:
        return 0

    c = comp_type.strip().lower()
    s = subject_type.strip().lower()

    if c == s:
        return 100

    NEAR_IDENTICAL = [
        {"apartment", "flat"},
        {"commercial_office", "office"},
        {"retail", "shop"},
    ]
    for pair in NEAR_IDENTICAL:
        if c in pair and s in pair:
            return 85

    CLOSE = [
        {"villa", "independent house"},
        {"villa", "plot"},
        {"independent house", "plot"},
        {"apartment", "residential"},
        {"flat", "residential"},
    ]
    for pair in CLOSE:
        if c in pair and s in pair:
            return 70

    BROAD = [
        {"office", "commercial"},
        {"commercial_office", "commercial"},
        {"retail", "commercial"},
        {"shop", "commercial"},
    ]
    for pair in BROAD:
        if c in pair and s in pair:
            return 40

    return 0


def _confidence_tier(score: int) -> str:
    if score >= 75: return "High"
    if score >= 55: return "Medium"
    if score >= 35: return "Low"
    return "Very Low"


# ── MAIN SCORING FUNCTION ─────────────────────────────────────────────────

def assign_confidence_score(
    comp: dict,
    subject_location: str,
    subject_property_type: str,
) -> dict:
    """
    Weights:
      40% → distance      (km-based, _score_distance)
      40% → location      (text/name-based, _score_location — NO km used)
      20% → property type (type field comparison)

    Neutral fallback (50) when subject values are empty so distance
    alone can still produce a High result.
    """
    dist_score = _score_distance(comp.get("distance_from_subject_km"))

    # ── Location score (text only, no km) ─────────────────────────────────
    if subject_location and subject_location.strip():
        loc_score    = _score_location(comp.get("location", ""), subject_location)
        loc_note     = f"vs subject '{subject_location}'"
        loc_fallback = False
    else:
        loc_score    = 50
        loc_note     = "subject location unknown → neutral 50"
        loc_fallback = True

    # ── Property type score ───────────────────────────────────────────────
    if subject_property_type and subject_property_type.strip():
        type_score    = _score_property_type(comp.get("property_type", ""), subject_property_type)
        type_note     = f"vs subject '{subject_property_type}'"
        type_fallback = False
    else:
        type_score    = 50
        type_note     = "subject property type unknown → neutral 50"
        type_fallback = True

    final_score = round(0.40 * dist_score + 0.40 * loc_score + 0.20 * type_score)

    # ── Diagnostic log ────────────────────────────────────────────────────
    logger.debug(
        f"[Confidence] '{comp.get('project_name', '?')}' | "
        f"dist={dist_score} (km={comp.get('distance_from_subject_km')}) | "
        f"loc={loc_score}{'*' if loc_fallback else ''} "
        f"('{comp.get('location', '')}') | "
        f"type={type_score}{'*' if type_fallback else ''} "
        f"('{comp.get('property_type', '')}') | "
        f"final={final_score}  (* = neutral fallback)"
    )

    dist_km  = comp.get("distance_from_subject_km")
    dist_str = f"{float(dist_km):.2f} km" if dist_km is not None else "unknown distance"

    reasoning = (
        f"Distance score {dist_score}/100 ({dist_str} from subject). "
        f"Location name score {loc_score}/100 — text match only, no km used "
        f"(comparable: '{comp.get('location', '—')}' {loc_note}). "
        f"Property type score {type_score}/100 "
        f"(comparable: '{comp.get('property_type', '—')}' {type_note})."
    )

    comp["confidence_score"]     = final_score
    comp["confidence_tier"]      = _confidence_tier(final_score)
    comp["confidence_reasoning"] = reasoning
    comp["factor_breakdown"]     = {
        "distance":           dist_score,
        "location":           loc_score,
        "property_type":      type_score,
        "loc_fallback_used":  loc_fallback,
        "type_fallback_used": type_fallback,
    }
    return comp


# ══════════════════════════════════════════════════════════════════════════
# ── Helpers ───────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _is_fuzzy_match(s1, s2):
    if not s1 or not s2:
        return False
    def clean(s):
        return re.sub(r'[^a-z0-9]', '', str(s).lower().strip())
    c1, c2 = clean(s1), clean(s2)
    if not c1 or not c2:
        return False
    if c1 == c2 or c1 in c2 or c2 in c1:
        return True
    words1 = set(str(s1).lower().split())
    words2 = set(str(s2).lower().split())
    intersection = words1 & words2
    union        = words1 | words2
    return bool(union) and len(intersection) / len(union) >= 0.5


def _is_subject_project(
    comp_name: str,
    distance_km: float | None,
    subject_name: str | None,
) -> bool:
    if not subject_name:
        return False
    if not _is_fuzzy_match(comp_name, subject_name):
        return False
    if distance_km is None:
        return True
    try:
        return float(distance_km) <= 0.15
    except (ValueError, TypeError):
        return True


def _extract_subject_from_rows(
    all_comps: list[dict],
    subject_project_name: str | None,
) -> dict | None:
    if not subject_project_name:
        return None
    for c in all_comps:
        if _is_subject_project(
            c["project_name"],
            c["distance_from_subject_km"],
            subject_project_name,
        ):
            return c
    return None


def _print_db_result(result: dict, subject_project_name: str = None) -> None:
    status = result.get("status", "unknown")
    count  = result.get("count", 0)
    error  = result.get("error")
    comps  = result.get("comparables", [])

    sep = "=" * 80
    print(f"\n{sep}")
    print(f"  DB COMPARABLE SEARCH RESULT")
    print(sep)
    print(f"  Status : {status.upper()}")
    print(f"  Count  : {count}")
    if error:
        print(f"  Error  : {error}")
    print(sep)

    if comps:
        headers = [
            "PROJECT ID", "PROJECT NAME", "LOCATION", "COUNTRY",
            "PROP TYPE", "CATEGORY", "DISTANCE",
            "CONFIDENCE", "TIER", "TRANSACTIONS", "LAT", "LNG",
        ]
        show_role = bool(subject_project_name and str(subject_project_name).strip())
        if show_role:
            headers.insert(2, "ROLE")

        rows = []
        for item in comps:
            dist_raw = item.get("distance_from_subject_km")
            dist_str = f"{float(dist_raw):.3f} km" if dist_raw is not None else "—"
            lat_str  = f"{item['map_search_lat']:.4f}" if item.get("map_search_lat") else "—"
            lng_str  = f"{item['map_search_lng']:.4f}" if item.get("map_search_lng") else "—"

            row = [
                str(item.get("project_id")              or "—"),
                str(item.get("project_name")            or "—"),
                str(item.get("location")                or "—"),
                str(item.get("country")                 or "—"),
                str(item.get("property_type")           or "—"),
                str(item.get("project_category")        or "—"),
                dist_str,
                str(item.get("confidence_score",  "—")),
                str(item.get("confidence_tier",   "—")),
                str(item.get("total_transaction_count") or "—"),
                lat_str,
                lng_str,
            ]
            if show_role:
                role = "Comparable"
                if _is_fuzzy_match(str(item.get("project_name", "")), subject_project_name):
                    try:
                        if (dist_raw is not None) and float(dist_raw) <= 0.15:
                            role = "Subject Project"
                    except (ValueError, TypeError):
                        role = "Subject Project"
                row.insert(2, role)
            rows.append(row)

        col_w = [len(h) for h in headers]
        for row in rows:
            for i, v in enumerate(row):
                col_w[i] = max(col_w[i], len(v))

        sep_row = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
        hdr_row = "|" + "|".join(
            f" {headers[i].ljust(col_w[i])} " for i in range(len(headers))
        ) + "|"
        print(sep_row)
        print(hdr_row)
        print(sep_row)
        for row in rows:
            print("|" + "|".join(
                f" {row[i].ljust(col_w[i])} " for i in range(len(row))
            ) + "|")
        print(sep_row)
    else:
        print("  (no comparables returned)")

    print(f"{sep}\n")
    sys.stdout.flush()


# ── Property type mapping ─────────────────────────────────────────────────
_PROP_TYPE_TO_DB_TERM = {
    "apartment":         "Flat",
    "flat":              "Flat",
    "commercial_office": "Office",
    "office":            "Office",
    "retail":            "Shop",
    "shop":              "Shop",
    "villa":             "Villa",
    "plot":              "Plot",
}


def _normalize_property_type_for_db(raw: str) -> str:
    cleaned = raw.strip().lower()
    if cleaned in ("villa", "plot"):
        return "either Villa or Plot"
    return _PROP_TYPE_TO_DB_TERM.get(cleaned, raw.strip().capitalize())


def _extract_json_array(text: str):
    match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    start, end = text.find('['), text.rfind(']')
    if start != -1 and end != -1 and end > start:
        for i in range(end, start, -1):
            try:
                return json.loads(text[start:i + 1])
            except Exception:
                continue
    return None


def _is_db_failure(text: str) -> bool:
    signals = [
        "max iterations", "no good verdict", "could not parse",
        "no project", "0 matching", "0 rows",
    ]
    return any(s in (text or "").lower() for s in signals)


def _build_query(lat: float, lng: float, property_type: str) -> str:
    db_term = _normalize_property_type_for_db(property_type)
    return (
        f"I need all unique projects within 3 km radius of this "
        f"Latitute is {lat} and Longitude {lng} "
        f"and property type is {db_term}. And expected columns should be "
        f"Project_id, Project_name, lat, long, Location, Country, "
        f"Property_type, Property_category, Distance, Total_transaction_count."
    )


def _get_field(item: dict, keys: list[str], default=None):
    for k in keys:
        for ik in item:
            if ik.strip().lower() == k.lower():
                v = item[ik]
                if v is not None and str(v).strip() != "":
                    return v
    return default


def _row_to_comparable(row: dict) -> dict:
    lat      = _get_field(row, ["project_latitude", "lat", "latitude"])
    lng      = _get_field(row, ["project_longitude", "long", "longitude"])
    distance = _get_field(row, ["distance", "distance_from_subject_km", "Distance"])

    try:    distance = float(distance)
    except: distance = None
    try:    lat = float(lat) if lat is not None else None
    except: lat = None
    try:    lng = float(lng) if lng is not None else None
    except: lng = None

    prop_raw = (_get_field(row, ["property_type", "Property_type", "type"]) or "").strip().lower()
    if prop_raw in ("flat", "apartment"):   canonical = "apartment"
    elif prop_raw == "office":              canonical = "commercial_office"
    elif prop_raw in ("shop", "retail"):    canonical = "retail"
    elif prop_raw == "villa":               canonical = "villa"
    elif prop_raw == "plot":                canonical = "plot"
    else:                                   canonical = prop_raw or "apartment"

    cat = _get_field(row, [
        "property_category", "transaction_category", "Property_category", "category",
    ]) or "Residential"

    return {
        "project_name":             _get_field(row, ["project_name", "Project_name", "name"]) or "—",
        "location":                 _get_field(row, ["location_name", "location", "Location"]) or "",
        "country":                  _get_field(row, ["country_name", "country", "Country"]) or "",
        "property_type":            canonical,   # ← used for type scoring
        "project_category":         cat,         # ← display only
        "age_years":                None,
        "possession_status":        "—",
        "source_url":               None,
        "reason":                   "Sourced from internal transaction database",
        "location_certainty":       "Sure",
        "map_search_lat":           lat,
        "map_search_lng":           lng,
        "geocode_source":           "internal_db",
        "distance_from_subject_km": distance,
        "total_transaction_count":  _get_field(row, [
            "total_transaction_count", "transaction_count", "transaction_cnt",
        ]),
        "project_id":               _get_field(row, ["project_id", "id", "Project_id"]),
        "data_source":              "Internal DB",
        "confidence_score":         None,
        "confidence_tier":          None,
        "confidence_reasoning":     None,
        "factor_breakdown":         None,
    }


# ══════════════════════════════════════════════════════════════════════════
# ── Main entry-point ──────────────────────────────────────────════════════
# ══════════════════════════════════════════════════════════════════════════

def fetch_db_comparables(
    lat: float,
    lng: float,
    property_type: str,
    backend_url: str = "http://localhost:8000",
    subject_project_name: str = None,
    subject_location: str = "",
    subject_category: str = "",   # kept for backward compat — not used for scoring
) -> dict:
    """
    Main entry-point.

    AUTO-EXTRACTION:
      Subject location and property_type are pulled directly from the DB row
      that matches subject_project_name (distance ≤ 0.15 km).
      subject_location arg is only used as fallback if subject not in DB.

    Scoring weights:
      40% distance     — km-based
      40% location     — text/name match only, NO km
      20% property type — type field comparison
    """
    query = _build_query(lat, lng, property_type)
    url   = f"{backend_url}/ask_stream_data_retrieval"
    logger.info(f"[DB Comparables] Query: {query}")

    use_in_process = False
    try:
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
        from agents.data_retrieval.pipeline import UniversalRealEstateAgent
        data_retrieval_agent = UniversalRealEstateAgent()
        stream = data_retrieval_agent.execute_stream(query, selected_domain="transaction")
        use_in_process = True
    except Exception as e:
        logger.warning(f"[DB Comparables] Falling back to HTTP: {e}")

    accumulated = ""
    result_rows = None

    def _consume(lines):
        nonlocal accumulated, result_rows
        for line in lines:
            if not line or not line.startswith("data: "):
                continue
            payload_str = line[len("data: "):].strip()
            if not payload_str:
                continue
            try:
                payload    = json.loads(payload_str)
                event_type = payload.get("type")
                if event_type == "done":
                    break
                if event_type == "result_set":
                    rows = (payload.get("content") or {}).get("rows") or []
                    if rows:
                        result_rows = rows
                        logger.info(f"[DB Comparables] result_set: {len(result_rows)} rows")
                elif event_type == "report_chunk":
                    chunk = payload.get("content") or ""
                    if chunk:
                        accumulated += chunk
            except Exception:
                pass

    if use_in_process:
        logger.info("[DB Comparables] Executing in-process")
        try:
            _consume(stream)
        except Exception as e:
            logger.error(f"[DB Comparables] In-process failed: {e}")
            return _error_out("In-process execution failed", subject_project_name)
    else:
        try:
            resp = requests.get(
                url,
                params={"question": query, "selected_domain": "transaction"},
                stream=True,
                timeout=120,
            )
            if resp.status_code != 200:
                return _error_out(f"HTTP {resp.status_code}", subject_project_name)
            _consume(resp.iter_lines(decode_unicode=True))
        except requests.exceptions.ConnectionError as e:
            logger.error(f"[DB Comparables] Connection error: {e}")
            return _error_out("Could not connect to backend server", subject_project_name)
        except Exception as e:
            logger.error(f"[DB Comparables] Unexpected error: {e}")
            return _error_out(str(e), subject_project_name)

    logger.info(
        f"[DB Comparables] Stream done — "
        f"result_rows={len(result_rows) if result_rows else None} "
        f"accumulated_len={len(accumulated)}"
    )

    rows = result_rows
    if not rows:
        if _is_db_failure(accumulated):
            logger.warning(f"[DB Comparables] Failure signal: {accumulated[:200]!r}")
            return _no_results_out(subject_project_name)
        rows = _extract_json_array(accumulated)
        if not rows:
            logger.warning(f"[DB Comparables] No JSON found: {accumulated[:300]!r}")
            return _no_results_out(subject_project_name)

    all_comps = [_row_to_comparable(r) for r in rows]

    # ── Auto-extract subject location & type from DB rows ─────────────────
    subject_project = _extract_subject_from_rows(all_comps, subject_project_name)

    if subject_project:
        effective_location      = subject_project.get("location") or subject_location
        effective_property_type = subject_project.get("property_type") or property_type
        logger.info(
            f"[DB Comparables] Subject found in DB: "
            f"name='{subject_project.get('project_name')}' | "
            f"location='{effective_location}' | "
            f"property_type='{effective_property_type}'"
        )
    else:
        effective_location      = subject_location
        effective_property_type = property_type
        logger.warning(
            f"[DB Comparables] Subject not in DB rows — "
            f"fallback: location='{effective_location}' type='{effective_property_type}'"
        )

    # ── Score all comps ───────────────────────────────────────────────────
    for c in all_comps:
        assign_confidence_score(c, effective_location, effective_property_type)

    logger.info(
        f"[DB Comparables] Scored {len(all_comps)} comps | "
        f"location='{effective_location}' type='{effective_property_type}' | "
        f"tiers={ {c['confidence_tier'] for c in all_comps} }"
    )

    _print_db_result(
        {"comparables": all_comps, "count": len(all_comps), "status": "success", "error": None},
        subject_project_name,
    )

    # ── Separate subject from comparables ─────────────────────────────────
    comparables = (
        [
            c for c in all_comps
            if not _is_subject_project(
                c["project_name"], c["distance_from_subject_km"], subject_project_name
            )
        ]
        if subject_project_name
        else all_comps
    )

    comparables.sort(key=lambda x: x.get("confidence_score") or 0, reverse=True)
    logger.info(f"[DB Comparables] Final comparables: {len(comparables)}")

    return {
        "comparables":     comparables,
        "count":           len(comparables),
        "status":          "success",
        "error":           None,
        "subject_project": subject_project,
    }


# ── Early-exit helpers ────────────────────────────────────────────────────
def _error_out(msg: str, subject_project_name: str) -> dict:
    out = {"comparables": [], "count": 0, "status": "error", "error": msg}
    _print_db_result(out, subject_project_name)
    return out


def _no_results_out(subject_project_name: str) -> dict:
    out = {
        "comparables": [],
        "count": 0,
        "status": "no_results",
        "error": "No projects found in DB",
    }
    _print_db_result(out, subject_project_name)
    return out


# ══════════════════════════════════════════════════════════════════════════
# ── Property-type normalisation (public helper) ───────────────────────────
# ══════════════════════════════════════════════════════════════════════════

_PROP_TYPE_ALIASES: dict[str, set[str]] = {
    "apartment":         {"apartment", "flat", "condo", "condominium", "penthouse"},
    "villa":             {"villa", "bungalow", "row house", "townhouse", "independent house", "independent villa", "house"},
    "plot":              {"plot", "land", "site", "residential plot", "na plot"},
    "retail":            {"shop", "retail", "showroom"},
    "commercial_office": {"office", "workspace", "coworking", "commercial_office"},
    "mixed_use":         {"mixed use", "mixed-use"},
}

_PROJECT_CATEGORY_DEFAULT: dict[str, str] = {
    "apartment":         "Residential",
    "villa":             "Villa",
    "plot":              "Plot",
    "retail":            "Commercial",
    "commercial_office": "Commercial",
    "mixed_use":         "Residential + Commercial",
}


def normalize_property_type(raw: str) -> str | None:
    """Map a free-text property type string to the canonical internal key."""
    if not raw:
        return None
    raw = raw.lower().strip()
    for k, aliases in _PROP_TYPE_ALIASES.items():
        if raw == k or raw in aliases:
            return k
    return None


# ══════════════════════════════════════════════════════════════════════════
# ── Haversine distance helper ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in km between two lat/lng points."""
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lng2 - lng1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ══════════════════════════════════════════════════════════════════════════
# ── LLM web-search comparable agent ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """You are a real-estate comparable-selection assistant.
Given a subject property, return a JSON object with a single key "comparables"
whose value is an array of comparable project objects.

Each object MUST have these keys (use null when unknown):
  project_name        – string, exact project name
  location            – string, locality / neighbourhood name
  country             – string
  property_type       – one of: apartment | villa | plot | retail | commercial_office
  project_category    – Residential | Commercial | Villa | Plot
  age_years           – number or null (approximate age in years)
  possession_status   – "Ready to Move" | "Under Construction" | "—"
  source_url          – string or null (a publicly verifiable URL)
  reason              – 1-sentence reason why this is comparable

Return ONLY the JSON. No prose, no markdown fences."""


def _build_user_prompt(subject: dict, radius_km: float, exclude_names: list[str]) -> str:
    prop_type = subject.get("property_type", "apartment")
    excl = ", ".join(f'"{n}"' for n in exclude_names) if exclude_names else "none"
    rate_hint = ""
    if subject.get("rate_basis") == "plot_land":
        rate_hint = "\nIMPORTANT: The subject is a Villa being valued via the Cost Approach — identify PLOT or LAND comparables (not built-up villas) so a plot/land rate can be derived."

    return (
        f"Subject property:\n"
        f"  Name         : {subject.get('project_name', 'Subject Property')}\n"
        f"  Location     : {subject.get('location_name', '')}, {subject.get('country', 'India')}\n"
        f"  Property type: {prop_type}\n"
        f"  Coordinates  : lat={subject.get('lat', 0)}, lng={subject.get('lng', 0)}\n"
        f"  Search radius: {radius_km} km\n"
        f"{rate_hint}\n"
        f"Already found (exclude these): [{excl}]\n\n"
        f"Find up to 8 real, verifiable comparable projects within {radius_km} km.\n"
        f"Prefer projects that have been sold/transacted in the last 2 years.\n"
        f"Do NOT include the subject project itself."
    )


def comparable_selection_agent(
    subject: dict,
    on_progress=None,
    run_logger=None,
    metrics=None,
) -> dict:
    """
    LLM web-search comparable finder (iterative radius expansion).

    Strategy:
      Round 1 → 2 km   (tight local comps)
      Round 2 → 5 km   (if < 3 unique comps after round 1)
      Round 3 → 10 km  (if < 3 unique comps after round 2)

    Args:
        subject     – dict with project_name, location_name, country,
                      property_type, lat, lng  (and optionally rate_basis)
        on_progress – optional callback(iteration, radius_km, comps_so_far, new_added)
        run_logger  – optional run-logger for debug artefacts
        metrics     – optional metrics object (not mutated here; caller adds iterations)

    Returns:
        {
          "comparables":   [list of comparable dicts with confidence scores],
          "iterations":    int,
          "iterations_log": [...],
          "_token_usage":  {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int},
        }
    """
    subject_lat  = subject.get("lat") or 0
    subject_lng  = subject.get("lng") or 0
    subject_loc  = subject.get("location_name", "")
    subject_type = subject.get("property_type", "apartment")

    RADII      = [2.0, 5.0, 10.0]
    MIN_COMPS  = 3
    MAX_COMPS  = 12

    all_comps: list[dict]  = []
    seen_names: set[str]   = set()
    token_usage            = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    iterations_log: list   = []
    iteration              = 0

    for radius_km in RADII:
        iteration += 1
        logger.info(f"[ComparableAgent] Iteration {iteration} | radius={radius_km} km | found_so_far={len(all_comps)}")

        # ── Call GPT ────────────────────────────────────────────────────
        user_prompt = _build_user_prompt(subject, radius_km, list(seen_names))
        new_comps: list[dict] = []

        try:
            response = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.1,
                max_tokens=2000,
                timeout=45,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
            )
            usage = response.usage
            if usage:
                token_usage["prompt_tokens"]     += usage.prompt_tokens
                token_usage["completion_tokens"] += usage.completion_tokens
                token_usage["total_tokens"]      += usage.total_tokens

            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(raw)
            candidates = parsed.get("comparables") or []

            if run_logger:
                try:
                    run_logger.save_text(
                        "comparable_search",
                        f"iter_{iteration}_radius_{radius_km}km",
                        json.dumps(parsed, indent=2),
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"[ComparableAgent] LLM call failed (iter {iteration}): {e}")
            candidates = []

        # ── Process each LLM candidate ───────────────────────────────────
        for cand in candidates:
            pname = (cand.get("project_name") or "").strip()
            if not pname:
                continue

            # Deduplicate
            pname_key = re.sub(r'[^a-z0-9]', '', pname.lower())
            if pname_key in seen_names:
                continue
            seen_names.add(pname_key)

            # Geocode
            loc   = cand.get("location", "") or subject_loc
            cntry = cand.get("country", "") or subject.get("country", "India")
            geo = search_coordinates(
                location_name=loc,
                country=cntry,
                project_name=pname,
                stage="Stage 3 - Comparable Geocoding",
            )
            comp_lat = geo.get("lat")
            comp_lng = geo.get("lng")
            geo_src  = geo.get("source", "unknown") if "lat" in geo else "geocode_failed"

            # Distance
            if comp_lat and comp_lng and subject_lat and subject_lng:
                dist_km = round(_haversine_km(subject_lat, subject_lng, comp_lat, comp_lng), 3)
            else:
                dist_km = None

            # Canonical property_type
            raw_type  = cand.get("property_type", subject_type)
            canonical = normalize_property_type(raw_type) or subject_type

            comp = {
                "project_name":             pname,
                "location":                 loc,
                "country":                  cntry,
                "property_type":            canonical,
                "project_category":         cand.get("project_category") or _PROJECT_CATEGORY_DEFAULT.get(canonical, "Residential"),
                "age_years":                cand.get("age_years"),
                "possession_status":        cand.get("possession_status") or "—",
                "source_url":               cand.get("source_url"),
                "reason":                   cand.get("reason", "Identified by LLM web search"),
                "location_certainty":       "Sure" if "lat" in geo else "Uncertain",
                "map_search_lat":           comp_lat,
                "map_search_lng":           comp_lng,
                "geocode_source":           geo_src,
                "distance_from_subject_km": dist_km,
                "total_transaction_count":  None,
                "project_id":               None,
                "data_source":              "Web",
                "confidence_score":         None,
                "confidence_tier":          None,
                "confidence_reasoning":     None,
                "factor_breakdown":         None,
            }

            # Apply confidence scoring
            assign_confidence_score(comp, subject_loc, subject_type)

            new_comps.append(comp)

            if len(all_comps) + len(new_comps) >= MAX_COMPS:
                break

        all_comps.extend(new_comps)

        log_entry = {
            "iteration":    iteration,
            "radius_km":    radius_km,
            "new_added":    len(new_comps),
            "comps_so_far": len(all_comps),
        }
        iterations_log.append(log_entry)

        if on_progress:
            try:
                on_progress(
                    iteration=iteration,
                    radius_km=radius_km,
                    comps_so_far=len(all_comps),
                    new_added=len(new_comps),
                )
            except Exception:
                pass

        logger.info(
            f"[ComparableAgent] Iter {iteration} done | "
            f"new={len(new_comps)} | total={len(all_comps)} | "
            f"tokens={token_usage['total_tokens']}"
        )

        # Stop expanding if we already have enough
        if len(all_comps) >= MIN_COMPS:
            break

        if iteration < len(RADII):
            time.sleep(0.5)  # gentle rate-limit buffer between rounds

    # Sort by confidence score descending
    all_comps.sort(key=lambda x: x.get("confidence_score") or 0, reverse=True)

    logger.info(
        f"[ComparableAgent] Complete | "
        f"total_comps={len(all_comps)} | "
        f"iterations={iteration} | "
        f"tokens={token_usage}"
    )

    return {
        "comparables":    all_comps,
        "iterations":     iteration,
        "iterations_log": iterations_log,
        "_token_usage":   token_usage,
    }