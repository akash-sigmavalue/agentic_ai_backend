from __future__ import annotations

"""
DB Transaction Search Tool
Fetches individual transactions for a comparable project from the internal
transaction database via the /ask_stream_data_retrieval SSE endpoint.

Used when a user selects a comparable project that has source = 'Internal DB'.
Returns a list of transaction dicts, each stamped with source = 'Internal DB'.
"""

import sys
import json
import re
import time
import logging
import requests

# Reconfigure stdout to use UTF-8 (essential on Windows to print unicode characters like ₹)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

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
    # Try finding "Result:" first to isolate the SQL output array
    result_idx = text.lower().find("result:")
    candidate_text = text[result_idx:] if result_idx != -1 else text

    # Bracket-balanced JSON array extractor.
    start = candidate_text.find("[")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(candidate_text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    val = json.loads(candidate_text[start: i + 1])
                    if isinstance(val, list):
                        return val
                except Exception:
                    pass
    return None


def print_table(transactions):
    if not transactions:
        print("No transactions found.")
        return
    
    headers = [
        "PROJECT",
        "TYPE",
        "PROPERTY CATEGORY",
        "LIST TYPE",
        "CURRENCY",
        "PRICE",
        "PRICE/SQFT",
        "AREA",
        "AREA TYPE",
        "FLOOR",
        "LOCATION",
        "SOURCE",
        "NET CARPET AREA (SQM)",
        "COUNTRY",
        "DATE"
    ]
    
    def get_field(item, keys_list, default="-"):
        for k in keys_list:
            for item_key in item.keys():
                if item_key.strip().lower() == k.lower():
                    val = item[item_key]
                    if val is not None and str(val).strip() != "":
                        return str(val)
        return default

    rows = []
    for item in transactions:
        project = get_field(item, ["project_name", "project", "name"])
        prop_type_raw = get_field(item, ["property_type_raw", "type_raw", "raw_type", "type"])
        property_type = get_field(item, ["property_type", "property_category", "category"])
        list_type = get_field(item, ["transaction_category", "list_type", "transaction_type"])
        price = get_field(item, ["agreement_price", "price", "amount"])
        area_sqm = get_field(item, ["net_carpet_area_sq_m", "area_sqm", "carpet_area", "net_carpet_area"])
        floor = get_field(item, ["floor_number", "floor"])
        location = get_field(item, ["location_name", "location"])
        country = get_field(item, ["country_name", "country"])
        date = get_field(item, ["transaction_date", "date"])
        
        # 1. Deterministic currency
        country_lower = country.strip().lower()
        if "india" in country_lower:
            currency = "₹"
        elif "dubai" in country_lower or "uae" in country_lower:
            currency = "AED"
        else:
            currency = ""
            
        # 2. Deterministic area calculation (NET CARPET AREA * 10.764)
        try:
            area_sqm_val = float(area_sqm)
            area = f"{(area_sqm_val * 10.764):.2f}"
        except (ValueError, TypeError):
            area = "-"

        # 3. Deterministic price/sqft calculation (PRICE / (NET CARPET AREA * 10.764))
        try:
            price_val = float(price)
            area_sqm_val = float(area_sqm)
            if area_sqm_val > 0:
                price_sqft = f"{(price_val / (area_sqm_val * 10.764)):.2f}"
            else:
                price_sqft = "-"
        except (ValueError, TypeError):
            price_sqft = "-"
            
        # 4. Deterministic area type
        area_type = "Carpet Area"
        
        # 5. Deterministic source
        source = "Internal DB"
        
        row = [
            project, prop_type_raw, property_type, list_type,
            currency, price, price_sqft, area, area_type, floor,
            location, source, area_sqm, country, date
        ]
        rows.append(row)
        
    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(val))
            
    # Format and print table
    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    
    print(sep)
    header_row = "|" + "|".join(f" {headers[i].ljust(col_widths[i])} " for i in range(len(headers))) + "|"
    print(header_row)
    print(sep)
    
    for row in rows:
        row_str = "|" + "|".join(f" {row[i].ljust(col_widths[i])} " for i in range(len(row))) + "|"
        try:
            print(row_str)
        except Exception:
            # Fallback for encoding errors in certain terminal setups
            print(row_str.encode('ascii', errors='replace').decode('ascii'))
        
    print(sep)


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
        f"and property_type is {db_property_type}, ordered by transaction_date descending "
        f"to get the most recent transactions first. "
        f"And expected columns should be project_name, property_type_raw, property_type, "
        f"transaction_category, unit_configuration, agreement_price, net_carpet_area_sq_m, "
        f"floor_number, location_name, country_name, transaction_date."
    )

    url = f"{agent_base_url}/ask_stream_data_retrieval"

    # Check if we can run in-process directly to avoid deadlocking single-threaded Uvicorn
    try:
        import sys
        import os
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
        from agents.data_retrieval.pipeline import UniversalRealEstateAgent
        data_retrieval_agent = UniversalRealEstateAgent()
        use_in_process = True
    except Exception as e:
        logger.warning(f"[DB Transactions] Fallback to HTTP because in-process import failed: {e}")
        use_in_process = False

    def _do_single_fetch():
        """Run one attempt at the SSE stream. Returns (result_rows, accumulated)."""
        _accumulated = ""
        _result_rows = None

        if use_in_process:
            logger.info("[DB Transactions] Executing in-process to avoid Uvicorn deadlock")
            stream = data_retrieval_agent.execute_stream(query, selected_domain="transaction")
            for line in stream:
                if not line:
                    continue
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
        else:
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

    # Print the table of retrieved transactions in the terminal (showing date)
    print(f"\n--- [Internal DB] Transaction table for project_id={project_id} (ordered by date desc) ---")
    print_table(raw_transactions)

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
            "transaction_date":   t.get("transaction_date") or t.get("date"),
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

