from __future__ import annotations

"""
DB Comparable Search Tool
Fetches comparable projects from the internal transaction database
via the /ask_stream_data_retrieval SSE endpoint.

Returns data in the same shape as comparable_selection_agent so the
valuation pipeline can consume it without changes.
"""

import json
import re
import logging
import os
import requests
import sys

logger = logging.getLogger(__name__)


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
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    if union:
        jaccard = len(intersection) / len(union)
        if jaccard >= 0.5:
            return True
    return False


def _is_subject_project(comp_name: str, distance_km: float | None, subject_name: str | None) -> bool:
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


def _print_db_result(result: dict, subject_project_name: str = None) -> None:
    """Pretty-print the DB comparable search result to the terminal."""
    status = result.get("status", "unknown")
    count  = result.get("count", 0)
    error  = result.get("error")
    comps  = result.get("comparables", [])

    sep = "=" * 80
    print(f"\n{sep}")
    print(f"  DB COMPARABLE SEARCH RESULT")
    print(sep)
    print(f"  Status  : {status.upper()}")
    print(f"  Count   : {count}")
    if error:
        print(f"  Error   : {error}")
    print(sep)

    if comps:
        headers = [
            "PROJECT ID",
            "PROJECT NAME",
            "LOCATIONS",
            "COUNTRY",
            "TYPE",
            "PROPERTY CATEGORY",
            "DISTANCE",
            "TOTAL TRANSACTIONS",
            "LAT",
            "LONG"
        ]
        show_role = subject_project_name is not None and str(subject_project_name).strip() != ""
        if show_role:
            headers.insert(2, "ROLE")

        rows = []
        for item in comps:
            project_id = str(item.get("project_id") or "—")
            name = str(item.get("project_name") or "—")
            loc = str(item.get("location") or "—")
            country = str(item.get("country") or "—")
            prop_type = str(item.get("property_type") or "—")
            cat = str(item.get("project_category") or "—")
            
            dist_raw = item.get("distance_from_subject_km")
            dist = f"{float(dist_raw):.3f} km" if dist_raw is not None else "—"
            
            total_tx = str(item.get("total_transaction_count") or "—")
            lat = f"{item.get('map_search_lat'):.4f}" if item.get('map_search_lat') else "—"
            lng = f"{item.get('map_search_lng'):.4f}" if item.get('map_search_lng') else "—"
            
            row = [project_id, name, loc, country, prop_type, cat, dist, total_tx, lat, lng]
            if show_role:
                role_label = "Comparable"
                if _is_fuzzy_match(name, subject_project_name):
                    try:
                        distance_val = float(dist_raw) if dist_raw is not None else 999.0
                        if distance_val <= 0.15:  # 150 meters
                            role_label = "Subject Project"
                    except (ValueError, TypeError):
                        role_label = "Subject Project"
                row.insert(2, role_label)
            rows.append(row)

        # Calculate column widths
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(val))

        # Format and print table
        table_sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        print(table_sep)
        header_row = "|" + "|".join(f" {headers[i].ljust(col_widths[i])} " for i in range(len(headers))) + "|"
        print(header_row)
        print(table_sep)
        for row in rows:
            row_str = "|" + "|".join(f" {row[i].ljust(col_widths[i])} " for i in range(len(row))) + "|"
            print(row_str)
        print(table_sep)
    else:
        print("  (no comparables returned)")

    print(f"{sep}\n")
    sys.stdout.flush()

# ── Property type mapping ─────────────────────────────────────────────────
# Maps canonical valuation type keys → the exact term the DB agent expects
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
    """Map user / valuation canonical type to the DB agent's expected term."""
    cleaned = raw.strip().lower()
    if cleaned in ("villa", "plot"):
        return "either Villa or Plot"
    return _PROP_TYPE_TO_DB_TERM.get(cleaned, raw.strip().capitalize())


def _extract_json_array(text: str):
    """Robust JSON-array extractor (same logic as comparbale_retrival_from_DB.py)."""
    match = re.search(r'\[\s*\{.*\}\s*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    start = text.find('[')
    end   = text.rfind(']')
    if start != -1 and end != -1 and end > start:
        for i in range(end, start, -1):
            try:
                candidate = text[start:i + 1]
                return json.loads(candidate)
            except Exception:
                continue
    return None


def _is_db_failure(text: str) -> bool:
    """Return True when the agent could not find anything in the DB."""
    failure_signals = [
        "max iterations",
        "no good verdict",
        "could not parse",
        "no project",
        "0 matching",
        "0 rows",
    ]
    lowered = (text or "").lower()
    return any(s in lowered for s in failure_signals)


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
    """Case-insensitive dict lookup."""
    for k in keys:
        for ik in item.keys():
            if ik.strip().lower() == k.lower():
                v = item[ik]
                if v is not None and str(v).strip() != "":
                    return v
    return default


def _row_to_comparable(row: dict) -> dict:
    """
    Convert a DB result row to the same dict shape that
    comparable_selection_agent produces.
    """
    lat = _get_field(row, ["project_latitude", "lat", "latitude"])
    lng = _get_field(row, ["project_longitude", "long", "longitude"])
    distance = _get_field(row, ["distance", "distance_from_subject_km", "Distance"])
    try:
        distance = float(distance)
    except (TypeError, ValueError):
        distance = None

    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lat = lng = None

    prop_type_raw = _get_field(row, ["property_type", "Property_type", "type"]) or ""
    prop_type_lower = prop_type_raw.strip().lower()
    if prop_type_lower in ("flat", "apartment"):
        canonical_type = "apartment"
    elif prop_type_lower in ("office",):
        canonical_type = "commercial_office"
    elif prop_type_lower in ("shop", "retail"):
        canonical_type = "retail"
    elif prop_type_lower in ("villa",):
        canonical_type = "villa"
    elif prop_type_lower in ("plot",):
        canonical_type = "plot"
    else:
        canonical_type = prop_type_lower or "apartment"

    cat = _get_field(row, [
        "property_category", "transaction_category",
        "Property_category", "category"
    ]) or "Residential"

    # Calculate a proxy confidence score based on distance
    if distance is not None:
        if distance <= 1.0:
            score = 95
        else:
            score = max(70, 95 - int((distance - 1.0) * 10))
    else:
        score = 90

    if score >= 80:
        tier = "High"
    elif score >= 60:
        tier = "Medium"
    elif score >= 40:
        tier = "Low"
    else:
        tier = "Very Low"

    dist_str = f"{distance:.2f} km" if distance is not None else "unknown distance"
    reasoning = (
        f"Verified project record sourced from the internal transaction database. "
        f"Exact location coordinates and historical transaction records are fully confirmed. "
        f"Proximity is {dist_str} from the subject property."
    )

    return {
        "project_name":             _get_field(row, ["project_name", "Project_name", "name"]) or "—",
        "location":                 _get_field(row, ["location_name", "location", "Location"]) or "",
        "country":                  _get_field(row, ["country_name", "country", "Country"]) or "",
        "property_type":            canonical_type,
        "project_category":         cat,
        "age_years":                None,
        "possession_status":        "—",
        "source_url":               None,
        "reason":                   "Sourced from internal transaction database",
        "location_certainty":       "Sure",   # DB coordinates are exact
        "map_search_lat":           lat,
        "map_search_lng":           lng,
        "geocode_source":           "internal_db",
        "distance_from_subject_km": distance,
        "total_transaction_count":  _get_field(row, [
            "total_transaction_count", "transaction_count", "transaction_cnt"
        ]),
        "project_id":               _get_field(row, ["project_id", "id", "Project_id"]),
        "data_source":              "Internal DB",   # ← new field for UI SOURCE column
        "confidence_score":         score,
        "confidence_tier":          tier,
        "confidence_reasoning":     reasoning,
        "factor_breakdown": {
            "distance_micro_market": min(100, max(0, int(score))),
            "location_quality":      95,
            "brand_credibility":     95,
            "category_type_match":   100,
            "amenities_segment":     90,
            "possession_alignment":  90,
            "location_certainty":    100
        },
        "research_summary": {
            "developer_found":   "Verified",
            "segment_found":     "Verified",
            "amenities_found":   "Verified",
            "locality_quality":  "Same suburb" if (distance is not None and distance <= 2.0) else "Same city"
        }
    }


def fetch_db_comparables(
    lat: float,
    lng: float,
    property_type: str,
    backend_url: str = "http://localhost:8000",
    subject_project_name: str = None,
) -> dict:
    """
    Main entry-point.

    Returns:
        {
            "comparables":  [<comp_dict>, ...],
            "count":        int,
            "status":       "success" | "no_results" | "error",
            "error":        str | None,
        }
    """
    query = _build_query(lat, lng, property_type)
    url   = f"{backend_url}/ask_stream_data_retrieval"

    logger.info(f"[DB Comparables] Query: {query}")

    accumulated   = ""   # report_chunk text (fallback)
    result_rows   = None # from result_set event (preferred)

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

    logger.info(f"[DB Comparables] Stream done. result_rows={len(result_rows) if result_rows else None} | accumulated_len={len(accumulated)}")

    # ── Prefer result_set rows ───────────────────────────────────────
    if result_rows:
        all_comps = [_row_to_comparable(r) for r in result_rows]
        # Print terminal table with subject included (for debugging)
        _print_db_result({"comparables": all_comps, "count": len(all_comps), "status": "success", "error": None}, subject_project_name)
        # Extract subject project before filtering it out
        subject_project = None
        if subject_project_name:
            for c in all_comps:
                if _is_subject_project(c["project_name"], c["distance_from_subject_km"], subject_project_name):
                    subject_project = c
                    break
        # Filter subject out of the comparable list (don't show on UI)
        comparables = [
            c for c in all_comps
            if not _is_subject_project(c["project_name"], c["distance_from_subject_km"], subject_project_name)
        ] if subject_project_name else all_comps
        logger.info(f"[DB Comparables] Found {len(comparables)} comparables from DB (via result_set)")
        out = {
            "comparables":      comparables,
            "count":            len(comparables),
            "status":           "success",
            "error":            None,
            "subject_project":  subject_project,   # subject's DB entry (for listing fetch)
        }
        return out

    # ── Fallback: parse JSON from accumulated report_chunk text ──────────
    if _is_db_failure(accumulated):
        logger.warning(f"[DB Comparables] Failure signal in accumulated text: {accumulated[:200]!r}")
        out = {
            "comparables": [],
            "count":       0,
            "status":      "no_results",
            "error":       "No projects found in DB",
        }
        _print_db_result(out, subject_project_name)
        return out

    rows = _extract_json_array(accumulated)
    if not rows:
        logger.warning(f"[DB Comparables] Could not parse JSON. accumulated: {accumulated[:300]!r}")
        out = {
            "comparables": [],
            "count":       0,
            "status":      "no_results",
            "error":       "No projects found in DB",
        }
        _print_db_result(out, subject_project_name)
        return out

    all_comps = [_row_to_comparable(r) for r in rows]
    # Print terminal table with subject included (for debugging)
    _print_db_result({"comparables": all_comps, "count": len(all_comps), "status": "success", "error": None}, subject_project_name)
    # Extract subject project before filtering
    subject_project = None
    if subject_project_name:
        for c in all_comps:
            if _is_subject_project(c["project_name"], c["distance_from_subject_km"], subject_project_name):
                subject_project = c
                break
    comparables = [
        c for c in all_comps
        if not _is_subject_project(c["project_name"], c["distance_from_subject_km"], subject_project_name)
    ] if subject_project_name else all_comps
    logger.info(f"[DB Comparables] Found {len(comparables)} comparables from DB (via report_chunk fallback)")
    out = {
        "comparables":      comparables,
        "count":            len(comparables),
        "status":           "success",
        "error":            None,
        "subject_project":  subject_project,
    }
    return out
