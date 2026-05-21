from __future__ import annotations

"""
DB Transaction Search Tool
Fetches individual transactions for a comparable project from the internal
transaction database via the /ask_stream_data_retrieval SSE endpoint.

Used when a user selects a comparable project that has source = 'Internal DB'.
Returns a list of transaction dicts, each stamped with source = 'Internal DB'.
"""

import json
import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

# ── Property type normalisation (mirrors transaction_retrival_from_DB.py) ──────
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


def _normalize_property_type(raw: str) -> str:
    cleaned = raw.strip().lower()
    if cleaned in ("villa", "plot"):
        return "either Villa or Plot"
    return _PROP_TYPE_TO_DB_TERM.get(cleaned, raw.strip().capitalize())


def _extract_json_array(text: str):
    """Bracket-balanced JSON array extractor."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start: i + 1])
                except Exception:
                    return None
    return None


def fetch_db_transactions(
    project_id: int | str,
    property_type: str,
    agent_base_url: str = "http://localhost:8000",
) -> dict:
    """
    Query the DB retrieval agent for all transactions belonging to
    `project_id` with the given `property_type`.

    Returns:
        {
            "status": "success" | "error" | "no_results",
            "transactions": [ {...}, ... ],   # each has source="Internal DB"
            "error": "..."                    # only on error/no_results
        }
    """
    db_property_type = _normalize_property_type(property_type)

    # Build the natural-language query for the agent
    pid_str = str(project_id).strip()
    if pid_str.isdigit():
        project_filter = f"project_id = {pid_str}"
    else:
        project_filter = f"project_name = '{pid_str}'"

    query = (
        f"I need all transactions where {project_filter} "
        f"and property_type is {db_property_type}. "
        f"And expected columns should be project_name, property_type_raw, property_type, "
        f"transaction_category, unit_configuration, agreement_price, net_carpet_area_sq_m, "
        f"floor_number, location_name, country_name."
    )

    url = f"{agent_base_url}/ask_stream_data_retrieval"

    def _do_single_fetch():
        """Run one attempt at the SSE stream. Returns (result_rows, accumulated)."""
        _accumulated = ""
        _result_rows = None

        resp = requests.get(
            url,
            params={"question": query, "selected_domain": "transaction"},
            stream=True,
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Agent returned HTTP {resp.status_code}")

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue
            payload_str = line[len("data:"):].strip()
            if not payload_str:
                continue
            try:
                payload = json.loads(payload_str)
            except Exception:
                continue

            event_type = payload.get("type", "")
            content    = payload.get("content", "")

            if event_type == "done":
                break

            # PRIMARY: capture structured rows directly
            if event_type == "result_set":
                rs = content if isinstance(content, dict) else {}
                rows = rs.get("rows") or []
                if rows:
                    _result_rows = rows
                    logger.info(f"[DB Transactions] result_set captured: {len(_result_rows)} rows")

            # FALLBACK: accumulate streamed text chunks
            elif event_type == "report_chunk" and content:
                _accumulated += str(content)

            elif event_type == "error":
                raise RuntimeError(str(content))

        return _result_rows, _accumulated

    # ── Retry loop (up to 3 attempts) ─────────────────────────────────────────
    # The data retrieval agent occasionally returns a premature `done` without
    # executing any SQL (empty stream). We retry with a short delay in that case.
    MAX_ATTEMPTS = 3
    result_rows = None
    accumulated = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result_rows, accumulated = _do_single_fetch()
            # If we got data, stop retrying
            if result_rows or accumulated:
                break
            # Empty stream — log and retry
            logger.warning(
                f"[DB Transactions] Attempt {attempt}/{MAX_ATTEMPTS}: empty stream for "
                f"project_id={project_id}. {'Retrying...' if attempt < MAX_ATTEMPTS else 'Giving up.'}"
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(2)
        except requests.exceptions.ConnectionError:
            return {
                "status": "error",
                "transactions": [],
                "error": "Could not connect to the agent backend.",
            }
        except RuntimeError as rte:
            return {
                "status": "error",
                "transactions": [],
                "error": str(rte),
            }
        except Exception as exc:
            logger.warning(f"[DB Transactions] Attempt {attempt} exception: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(2)
            else:
                return {
                    "status": "error",
                    "transactions": [],
                    "error": str(exc),
                }

    # ── Parse transactions ─────────────────────────────────────────────────
    # Prefer result_set rows; fall back to JSON in accumulated text
    if result_rows:
        raw_transactions = result_rows
        logger.info(f"[DB Transactions] Using {len(raw_transactions)} rows from result_set")
    else:
        raw_transactions = _extract_json_array(accumulated)
        if not raw_transactions:
            logger.warning(f"[DB Transactions] No data after {MAX_ATTEMPTS} attempt(s). accumulated: {accumulated[:300]!r}")
            return {
                "status": "no_results",
                "transactions": [],
                "error": "No transactions found for this project.",
            }

    # ── Derive computed columns + stamp source ─────────────────────────────
    enriched = []
    for t in raw_transactions:
        country = str(t.get("country_name") or "India")
        country_lower = country.strip().lower()
        if "india" in country_lower:
            currency = "₹"
        elif "dubai" in country_lower or "uae" in country_lower:
            currency = "AED"
        else:
            currency = "₹"

        # area in sqft
        try:
            area_sqm = float(t.get("net_carpet_area_sq_m") or 0)
            area_sqft = round(area_sqm * 10.764, 2) if area_sqm else None
        except (ValueError, TypeError):
            area_sqft = None
            area_sqm = None

        # price / sqft
        try:
            price_val = float(t.get("agreement_price") or 0)
            if area_sqft and area_sqft > 0:
                price_per_sqft = round(price_val / area_sqft, 2)
            else:
                price_per_sqft = None
        except (ValueError, TypeError):
            price_val = None
            price_per_sqft = None

        enriched.append({
            # Agent-fetched columns
            "project_name":       t.get("project_name"),
            "property_type_raw":  t.get("property_type_raw"),
            "property_type":      t.get("property_type"),
            "transaction_category": t.get("transaction_category"),
            "unit_configuration": t.get("unit_configuration"),
            "agreement_price":    t.get("agreement_price"),
            "net_carpet_area_sq_m": t.get("net_carpet_area_sq_m"),
            "floor_number":       t.get("floor_number"),
            "location_name":      t.get("location_name"),
            "country_name":       country,
            # Derived columns (deterministic)
            "currency":           currency,
            "area_sqft":          area_sqft,
            "price_per_sqft":     price_per_sqft,
            "area_type":          "Carpet Area",
            # Source
            "source":             "Internal DB",
        })

    logger.info(
        f"[DB Transactions] project_id={project_id} property_type={property_type} "
        f"-> {len(enriched)} transactions"
    )
    return {"status": "success", "transactions": enriched}
