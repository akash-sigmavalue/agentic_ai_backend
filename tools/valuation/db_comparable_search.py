from __future__ import annotations

"""
DB Comparable Search Tool
Fetches comparable projects from the internal transaction database
via the /ask_stream_data_retrieval SSE endpoint.

Confidence scoring (pure Python, no LLM):
  - Distance      : 40% weight
  - Location      : 40% weight  → auto-extracted from subject project in DB results
  - Property Type : 20% weight  → uses property_type (apartment/villa/plot/office/retail)
                                   NOT project_category (Residential/Commercial)

KEY CHANGES vs previous version:
  1. Category scoring now compares property_type field (e.g. "apartment" vs "apartment")
     instead of project_category (e.g. "Residential" vs "Residential").
     This gives exact 100/100 matches for same-type comps automatically.

  2. subject_location is now AUTO-EXTRACTED from the subject project found inside
     the DB results (the row with distance ≤ 0.15 km matching subject_project_name).
     Caller no longer needs to pass subject_location manually.
     Fallback: if subject project not found in DB, uses caller-supplied subject_location.

  3. subject_property_type similarly auto-extracted from subject project row.
     Fallback: caller-supplied property_type argument.

  4. Neutral fallback (50) still applied when subject values are genuinely unknown.
"""

import json
import re
import logging
import os
import requests
import sys

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# ── Confidence Scoring ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _score_location(comp_location: str, subject_location: str) -> int:
    """
    100 → exact match
     85 → first token + one more shared token  (e.g. "Mundhwa Pune" vs "Mundhwa East Pune")
     80 → first token matches  (same micro-market)
     70 → 2+ shared tokens
     55 → 1 shared token
     25 → same city only  (floor — prevents hard 0 for nearby city comps)
      0 → no relation at all
    """
    if not comp_location or not subject_location:
        return 0

    STOP = {
        "the", "a", "an", "of", "in", "at", "near", "and", "by",
        "sector", "phase", "block", "tower", "wing",
    }

    def tokens(s: str) -> list[str]:
        return [
            w for w in re.split(r'[\s,\-/]+', s.lower().strip())
            if len(w) > 2 and w not in STOP
        ]

    c_tok = tokens(comp_location)
    s_tok = tokens(subject_location)

    if not c_tok or not s_tok:
        return 0

    if comp_location.strip().lower() == subject_location.strip().lower():
        return 100

    shared = set(c_tok) & set(s_tok)
    first  = c_tok[0] == s_tok[0]

    if first and len(shared) >= 2: return 85
    if first:                      return 80
    if len(shared) >= 2:           return 70
    if len(shared) == 1:           return 55

    CITY_WORDS = {
        "pune", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai",
        "delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad",
        "dubai", "abudhabi", "sharjah", "singapore", "london", "toronto",
    }
    if (set(c_tok) & CITY_WORDS) & (set(s_tok) & CITY_WORDS):
        return 25

    return 0


def _score_distance(distance_km: float | None) -> int:
    """
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
    Scores based on property_type field (apartment / villa / plot / office / retail …)
    NOT project_category (Residential / Commercial).

    100 → exact same type
     85 → near-identical  (apartment ↔ flat)
     70 → closely related (villa ↔ independent house, plot ↔ villa)
     40 → same broad class (office ↔ commercial_office, shop ↔ retail)
      0 → unrelated       (apartment ↔ office)
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
    Assign confidence_score, confidence_tier, confidence_reasoning,
    and factor_breakdown to a comparable dict.

    Args:
        comp                  — comparable dict (from _row_to_comparable)
        subject_location      — location string of subject project
                                (auto-extracted from DB; caller-supplied as fallback)
        subject_property_type — property_type of subject project
                                (auto-extracted from DB; caller-supplied as fallback)

    Neutral fallback (50) is used when subject values are empty so that
    distance alone can still produce a High result.
    """
    dist_score = _score_distance(comp.get("distance_from_subject_km"))

    # ── Location score ────────────────────────────────────────────────────
    if subject_location and subject_location.strip():
        loc_score    = _score_location(comp.get("location", ""), subject_location)
        loc_note     = f"vs subject '{subject_location}'"
        loc_fallback = False
    else:
        loc_score    = 50
        loc_note     = "subject location unknown → neutral 50"
        loc_fallback = True

    # ── Property type score  (uses property_type, NOT project_category) ───
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
        f"Location score {loc_score}/100 "
        f"(comparable: '{comp.get('location', '—')}' {loc_note}). "
        f"Property type score {type_score}/100 "
        f"(comparable: '{comp.get('property_type', '—')}' {type_note})."
    )

    comp["confidence_score"]     = final_score
    comp["confidence_tier"]      = _confidence_tier(final_score)
    comp["confidence_reasoning"] = reasoning
    comp["factor_breakdown"]     = {
        "distance":            dist_score,
        "location":            loc_score,
        "property_type":       type_score,
        "loc_fallback_used":   loc_fallback,
        "type_fallback_used":  type_fallback,
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
    """
    Find the subject project inside the DB result rows.
    Returns the comp dict if found, else None.
    """
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
                str(item.get("project_id")             or "—"),
                str(item.get("project_name")           or "—"),
                str(item.get("location")               or "—"),
                str(item.get("country")                or "—"),
                str(item.get("property_type")          or "—"),
                str(item.get("project_category")       or "—"),
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
        "property_type":            canonical,   # ← used for scoring
        "project_category":         cat,         # ← kept for display only
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
    subject_category: str = "",          # kept for backward compat — no longer used for scoring
) -> dict:
    """
    Main entry-point.

    subject_location      — fallback if subject project not found in DB results.
                            Best practice: pass it; it will be overridden by the
                            DB row's location when the subject is found automatically.

    subject_category      — kept for backward compatibility but no longer used
                            for scoring. Scoring now uses property_type instead.

    property_type         — used both to query the DB AND as fallback subject
                            property type when subject row not found in results.

    AUTO-EXTRACTION:
      After fetching DB rows, this function looks for the subject project
      (name fuzzy-match + distance ≤ 0.15 km) and pulls its location and
      property_type directly from the DB row.  This means:
        • subject_location  is auto-filled  (no more manual passing needed)
        • subject_property_type is auto-filled from the DB row

    Returns:
        {
            "comparables":     [...],   # sorted by confidence_score desc
            "count":           int,
            "status":          "success" | "no_results" | "error",
            "error":           str | None,
            "subject_project": dict | None,
        }
    """
    query = _build_query(lat, lng, property_type)
    url   = f"{backend_url}/ask_stream_data_retrieval"
    logger.info(f"[DB Comparables] Query: {query}")

    # Check if we can run in-process directly to avoid deadlocking single-threaded Uvicorn
    try:
        import sys
        import os
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
        from agents.data_retrieval.pipeline import UniversalRealEstateAgent
        data_retrieval_agent = UniversalRealEstateAgent()
        stream = data_retrieval_agent.execute_stream(query, selected_domain="transaction")
        use_in_process = True
    except Exception as e:
        logger.warning(f"[DB Comparables] Fallback to HTTP because in-process import failed: {e}")
        use_in_process = False

    accumulated   = ""   # report_chunk text (fallback)
    result_rows   = None # from result_set event (preferred)

    if use_in_process:
        logger.info("[DB Comparables] Executing in-process to avoid Uvicorn deadlock")
        try:
            for line in stream:
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                payload_str = line[len("data: "):].strip()
                if not payload_str:
                    continue
                try:
                    payload = json.loads(payload_str)
                    event_type = payload.get("type")

                    if event_type == "done":
                        break

                    if event_type == "result_set":
                        rs = payload.get("content") or {}
                        rows = rs.get("rows") or []
                        if rows:
                            result_rows = rows
                            logger.info(f"[DB Comparables] result_set captured: {len(result_rows)} rows")

                    elif event_type == "report_chunk":
                        chunk = payload.get("content") or ""
                        if chunk:
                            accumulated += chunk
                            logger.debug(f"[DB Comparables] report_chunk: {chunk[:80]!r}")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[DB Comparables] In-process execution failed: {e}")
            out = {
                "comparables": [],
                "count":       0,
                "status":      "error",
                "error":       str(e),
            }
            _print_db_result(out, subject_project_name)
            return out
    else:
        try:
            response = requests.get(
                url,
                params={"question": query, "selected_domain": "transaction"},
                stream=True,
                timeout=120,
            )
            if response.status_code != 200:
                out = {
                    "comparables": [],
                    "count":       0,
                    "status":      "error",
                    "error":       f"HTTP {response.status_code}",
                }
                _print_db_result(out, subject_project_name)
                return out

            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                payload_str = line[len("data: "):].strip()
                if not payload_str:
                    continue
                try:
                    payload = json.loads(payload_str)
                    event_type = payload.get("type")

                    # ── Stop reading once the stream signals completion ────
                    if event_type == "done":
                        break

                    # ── PRIMARY: capture result_set rows directly ──────────
                    if event_type == "result_set":
                        rs = payload.get("content") or {}
                        rows = rs.get("rows") or []
                        if rows:
                            result_rows = rows
                            logger.info(f"[DB Comparables] result_set captured: {len(result_rows)} rows")

                    # ── FALLBACK: accumulate report_chunk text ─────────────
                    elif event_type == "report_chunk":
                        chunk = payload.get("content") or ""
                        if chunk:
                            accumulated += chunk
                            logger.debug(f"[DB Comparables] report_chunk: {chunk[:80]!r}")

                except Exception:
                    pass

        except requests.exceptions.ConnectionError as e:
            logger.error(f"[DB Comparables] Connection error: {e}")
            out = {
                "comparables": [],
                "count":       0,
                "status":      "error",
                "error":       "Could not connect to backend server",
            }
            _print_db_result(out, subject_project_name)
            return out
        except Exception as e:
            logger.error(f"[DB Comparables] Unexpected error: {e}")
            out = {
                "comparables": [],
                "count":       0,
                "status":      "error",
                "error":       str(e),
            }
            _print_db_result(out, subject_project_name)
            return out

    logger.info(
        f"[DB Comparables] Stream done — "
        f"result_rows={len(result_rows) if result_rows else None} "
        f"accumulated_len={len(accumulated)}"
    )

    # ── Parse rows ────────────────────────────────────────────────────────
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

    # ── AUTO-EXTRACT subject location & property_type from DB rows ────────
    subject_project = _extract_subject_from_rows(all_comps, subject_project_name)

    if subject_project:
        # Use what the DB actually says about the subject — most accurate
        effective_location      = subject_project.get("location") or subject_location
        effective_property_type = subject_project.get("property_type") or property_type
        logger.info(
            f"[DB Comparables] Subject found in DB rows: "
            f"name='{subject_project.get('project_name')}' | "
            f"location='{effective_location}' | "
            f"property_type='{effective_property_type}'"
        )
    else:
        # Fall back to caller-supplied values
        effective_location      = subject_location
        effective_property_type = property_type
        logger.warning(
            f"[DB Comparables] Subject project NOT found in DB rows — "
            f"using caller-supplied: location='{effective_location}' "
            f"property_type='{effective_property_type}'"
        )

    # ── Assign confidence scores ───────────────────────────────────────────
    for c in all_comps:
        assign_confidence_score(c, effective_location, effective_property_type)

    logger.info(
        f"[DB Comparables] Scored {len(all_comps)} comps | "
        f"effective_location='{effective_location}' "
        f"effective_property_type='{effective_property_type}' | "
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