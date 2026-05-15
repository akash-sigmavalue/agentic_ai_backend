"""
plot_rate_pipeline.py
=====================
Post-pipeline step: enriches cleaned listings with Plot Area Rate data.

Call `calculate_plot_rates(...)` AFTER `data_cleaning_pipeline(...)` returns,
but ONLY when the subject property_type is "plot".

Each cleaned listing gets four new fields:
  - plot_fsi_range              : {"low": float, "high": float, "best": float}
  - plot_construction_cost_range: {"low": float, "high": float, "best": float, "currency": str}
  - plot_derived_rate_range     : {"low": float, "high": float, "currency": str}
  - plot_derived_rate_per_sqft  : float  (midpoint — the single headline number)

Listings where price or area is null are skipped and get null for all four fields.

Model : gpt-4o-mini
"""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger("plot_rate_pipeline")

_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
PLOT_RATE_SYSTEM_PROMPT = """You are a senior real-estate analyst specializing in land valuation with deep knowledge of:
  - Construction costs across all global markets
  - FSI / FAR (Floor Space Index / Floor Area Ratio) norms by country, city, and zone type
  - Local currencies and formatting conventions

For EACH property in the input JSON array, estimate the parameters needed to reverse-engineer the Plot Area Rate.

INPUT per item:
  id             - identifier
  property_type  - (e.g., Villa, Apartment, House)
  built_up_sqft  - super built-up area
  total_price    - property price (in local currency)
  location       - locality / city
  country        - country name

STEPS for EACH item:
  1. Identify Local Currency: Code (e.g. INR) and Symbol (e.g. ₹) from the country.
  2. Estimate FSI/FAR: Typical range (low/high) and best estimate for this property type and location. 
     Briefly explain the reasoning (local norms, zone type).
  3. Estimate Construction Cost/sqft: Typical range (low/high) and best estimate in local currency.
     Factor in finish quality for the property type and 2025-2026 market conditions.
     Provide a brief rationale.

OUTPUT — strict JSON, no markdown fences:
{
  "results": [
    {
      "id": 0,
      "currency_code": "INR",
      "currency_symbol": "₹",
      "fsi_low": 1.0,
      "fsi_high": 1.5,
      "fsi_best": 1.25,
      "fsi_reasoning": "Standard residential FSI in Pune...",
      "const_cost_low": 2500,
      "const_cost_high": 3500,
      "const_cost_best": 3000,
      "const_cost_rationale": "2025 market rates for premium villas..."
    }
  ]
}

RULES:
- If total_price or built_up_sqft is null/0, return null for all numeric fields.
- Focus on providing MINIMIZED, high-confidence ranges.
- Return ONLY the JSON object.
"""


def _build_batch_payload(items: List[Dict]) -> str:
    """Serialises a batch of items for the LLM user message."""
    return json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LLM call with retry
# ---------------------------------------------------------------------------

def _call_llm_batch(
    items: List[Dict],
    metrics: Dict,
    metrics_lock: threading.Lock,
) -> List[Dict]:
    """Calls GPT-4o-mini for one batch; returns list of result dicts."""

    user_content = _build_batch_payload(items)

    for attempt in range(3):
        try:
            response = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": PLOT_RATE_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_content},
                ],
            )

            with metrics_lock:
                metrics["prompt_tokens"]     += response.usage.prompt_tokens
                metrics["completion_tokens"] += response.usage.completion_tokens
                metrics["total_tokens"]      += response.usage.total_tokens

            raw = response.choices[0].message.content
            parsed = json.loads(raw)
            return parsed.get("results", [])

        except Exception as exc:
            logger.warning(f"Plot-rate LLM batch attempt {attempt + 1} failed: {exc}")
            time.sleep(2 ** attempt)   # 1 s, 2 s, 4 s back-off

    logger.error("Plot-rate batch failed all 3 attempts — returning empty.")
    return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def calculate_plot_rates(
    pipeline_output: Dict,
    subject: Dict,
    location: str,
    country: str,
    property_type: str = "plot",
    batch_size: int = 8,
    on_progress=None,
) -> Dict:
    """
    Enriches `pipeline_output["cleaned_listings"]` with plot-rate fields.

    Parameters
    ----------
    pipeline_output : dict
        The dict returned by `data_cleaning_pipeline(...)`.
    subject : dict
        Subject property dict (used for location fallback if needed).
    location : str
        Human-readable locality / city for the LLM (e.g. "Baner, Pune").
    country : str
        Country name (e.g. "India").
    property_type : str
        Only runs when this equals "plot" (case-insensitive).
    batch_size : int
        Number of listings per LLM call (default 8).
    on_progress : callable | None
        Optional callback(event_key, message).

    Returns
    -------
    dict
        Same structure as `pipeline_output` with:
          - cleaned_listings enriched with plot-rate fields
          - audit_stats["plot_rate_token_usage"] added
          - audit_stats["plot_rate_skipped"] count added
    """

    # We now run this for any property type to ensure row-level plot-rate derivation
    # logic (reverse-engineering for buildings, direct for plots) is applied.
    # The intelligent split happens inside the loop below.
    # if property_type.strip().lower() != "plot":
    #     logger.info("calculate_plot_rates: skipped — property_type is not 'plot'.")
    #     return pipeline_output

    cleaned = pipeline_output.get("cleaned_listings", [])

    if not cleaned:
        logger.info("calculate_plot_rates: no cleaned listings to process.")
        return pipeline_output

    if on_progress:
        on_progress("plot_rate_start", f"Starting plot rate calculation for {len(cleaned)} listings")
    print(f"🏗️  [Plot Rate] Starting for {len(cleaned)} cleaned listings …")

    # ── Split into processable vs. skippable ──────────────────────────────
    processable: List[Tuple[int, Dict]] = []   # (original index, listing)
    skipped_count = 0

    for orig_idx, lst in enumerate(cleaned):
        price = lst.get("cleaned_price_value")
        area  = lst.get("final_super_builtup_area")
        ptype = str(lst.get("property_type") or "").strip().lower()

        if not price or not area or float(price) <= 0 or float(area) <= 0:
            _stamp_null_plot_fields(cleaned[orig_idx])
            skipped_count += 1
            continue

        if ptype == "plot":
            # Already a plot - use direct rate, skip LLM
            _stamp_direct_plot_fields(cleaned[orig_idx])
        else:
            # Apartment, Villa, etc. - queue for LLM reverse-engineering
            processable.append((orig_idx, lst))

    print(f"   → {len(processable)} listings queued, {skipped_count} skipped (null price/area)")

    # ── Build LLM input items ─────────────────────────────────────────────
    llm_items: List[Dict] = []
    for seq_id, (orig_idx, lst) in enumerate(processable):
        price = lst.get("cleaned_price_value")
        area  = lst.get("final_super_builtup_area")
        llm_items.append({
            "id":            seq_id,
            "property_type": ptype,
            "project_name":  lst.get("project_name", "Unknown"),
            "total_price":   price,
            "built_up_sqft": area,
            "location":      location or subject.get("location", "Unknown"),
            "country":       country,
        })
    # ── Batch ─────────────────────────────────────────────────────────────
    batches: List[List[Dict]] = [
        llm_items[i : i + batch_size] for i in range(0, len(llm_items), batch_size)
    ]

    metrics = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    metrics_lock = threading.Lock()

    # seq_id → result dict from LLM
    results_by_id: Dict[int, Dict] = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(_call_llm_batch, batch, metrics, metrics_lock): batch_idx
            for batch_idx, batch in enumerate(batches)
        }
        for future in as_completed(future_map):
            batch_idx = future_map[future]
            try:
                batch_results = future.result()
                for r in batch_results:
                    if r.get("id") is not None:
                        results_by_id[int(r["id"])] = r
                if on_progress:
                    on_progress(
                        f"plot_rate_batch_{batch_idx}",
                        f"Processed plot-rate batch {batch_idx + 1}/{len(batches)}",
                    )
                print(f"   ✓ Batch {batch_idx + 1}/{len(batches)} done")
            except Exception as exc:
                logger.error(f"Plot-rate batch {batch_idx} exception: {exc}")

    # ── Merge results back into cleaned listings ──────────────────────────
    for seq_id, (orig_idx, _) in enumerate(processable):
        result = results_by_id.get(seq_id)
        if result:
            _stamp_plot_fields(cleaned[orig_idx], result)
        else:
            _stamp_null_plot_fields(cleaned[orig_idx])

    # ── Audit stats ───────────────────────────────────────────────────────
    pipeline_output["audit_stats"]["plot_rate_token_usage"] = metrics
    pipeline_output["audit_stats"]["plot_rate_skipped"]     = skipped_count
    pipeline_output["audit_stats"]["plot_rate_processed"]   = len(processable)

    if on_progress:
        on_progress("plot_rate_done", "Plot rate calculation complete")
    print(f"✨ [Plot Rate] Done. Tokens used: {metrics['total_tokens']:,}")

    return pipeline_output


# ---------------------------------------------------------------------------
# Calculation Logic
# ---------------------------------------------------------------------------

def _calculate_residual_rate(
    price: float,
    built_up: float,
    fsi_low: float,
    fsi_high: float,
    fsi_best: float,
    cost_low: float,
    cost_high: float,
    cost_best: float,
) -> Dict:
    """
    Reverse-engineers Plot Rate using the Residual Method.
    Matching the logic in plot_rate_calculator.py.
    """
    if not price or not built_up or built_up <= 0:
        return {"error": "Invalid input"}

    # 1. Plot Area Range (Area = Built-up / FSI)
    # Lower FSI means larger land area.
    area_max = built_up / fsi_low if fsi_low > 0 else built_up
    area_min = built_up / fsi_high if fsi_high > 0 else built_up
    area_best = built_up / fsi_best if fsi_best > 0 else built_up

    # 2. Plot Value Range (Value = Price - Construction Cost)
    # Higher construction cost means lower land value.
    val_min = price - (cost_high * built_up)
    val_max = price - (cost_low * built_up)
    val_best = price - (cost_best * built_up)

    if val_max < 0:
        return {"negative_value": True}

    # 3. Plot Rate Range (Rate = Value / Area)
    # Conservative (Low) = Val Min / Area Max
    # Optimistic (High) = Val Max / Area Min
    rate_low = val_min / area_max if area_max > 0 else 0
    rate_high = val_max / area_min if area_min > 0 else 0
    rate_best = val_best / area_best if area_best > 0 else 0

    # Ensure no negatives in rates
    rate_low = max(0, rate_low)
    rate_high = max(0, rate_high)
    rate_best = max(0, rate_best)

    return {
        "rate_low": round(rate_low, 2),
        "rate_high": round(rate_high, 2),
        "rate_best": round(rate_best, 2),
        "negative_value": False
    }


# ---------------------------------------------------------------------------
# Field stamping helpers
# ---------------------------------------------------------------------------

def _stamp_plot_fields(listing: Dict, result: Dict) -> None:
    """Writes structured plot-rate fields onto a listing dict."""

    price = listing.get("cleaned_price_value")
    area = listing.get("final_super_builtup_area")

    # LLM Estimates
    fsi_low = result.get("fsi_low")
    fsi_high = result.get("fsi_high")
    fsi_best = result.get("fsi_best")
    cost_low = result.get("const_cost_low")
    cost_high = result.get("const_cost_high")
    cost_best = result.get("const_cost_best")

    listing["plot_fsi_range"] = {
        "low": fsi_low,
        "high": fsi_high,
        "best": fsi_best,
        "reasoning": result.get("fsi_reasoning"),
    }

    currency = result.get("currency_symbol") or result.get("currency_code") or listing.get("cleaned_currency") or "₹"

    listing["plot_construction_cost_range"] = {
        "low": cost_low,
        "high": cost_high,
        "best": cost_best,
        "currency": currency,
        "rationale": result.get("const_cost_rationale"),
    }

    # Perform Deterministic Calculation
    calc = _calculate_residual_rate(
        price=float(price or 0),
        built_up=float(area or 0),
        fsi_low=float(fsi_low or 0),
        fsi_high=float(fsi_high or 0),
        fsi_best=float(fsi_best or 0),
        cost_low=float(cost_low or 0),
        cost_high=float(cost_high or 0),
        cost_best=float(cost_best or 0),
    )

    neg = calc.get("negative_value", False)

    if neg or calc.get("rate_best") is None:
        listing["plot_derived_rate_range"] = None
        listing["plot_derived_rate_per_sqft"] = None
        listing["plot_negative_value_flag"] = True
    else:
        listing["plot_derived_rate_range"] = {
            "low": calc.get("rate_low"),
            "high": calc.get("rate_high"),
            "currency": currency,
        }
        listing["plot_derived_rate_per_sqft"] = calc.get("rate_best")
        listing["plot_negative_value_flag"] = False

    logger.info(f"FSI Range: {listing['plot_fsi_range']}")
    logger.info(f"Construction Cost Range: {listing['plot_construction_cost_range']}")
    logger.info(f"Derived Plot Rate: {listing['plot_derived_rate_per_sqft']} (Neg: {neg})")


def _stamp_null_plot_fields(listing: Dict) -> None:
    """Stamps null plot-rate fields for listings that couldn't be processed."""
    listing["plot_fsi_range"]               = None
    listing["plot_construction_cost_range"] = None
    listing["plot_derived_rate_range"]      = None
    listing["plot_derived_rate_per_sqft"]   = None
    listing["plot_negative_value_flag"]     = None


def _stamp_direct_plot_fields(listing: Dict) -> None:
    """Stamps fields for properties that are already plots (no reverse-engineering needed)."""
    price = listing.get("cleaned_price_value")
    area  = listing.get("final_super_builtup_area")
    currency = listing.get("cleaned_currency") or "₹"

    listing["plot_fsi_range"] = {"low": 1.0, "high": 1.0, "best": 1.0}
    listing["plot_construction_cost_range"] = {"low": 0, "high": 0, "best": 0, "currency": currency}

    if price and area and float(area) > 0:
        rate = round(float(price) / float(area), 2)
        listing["plot_derived_rate_range"] = {"low": rate, "high": rate, "currency": currency}
        listing["plot_derived_rate_per_sqft"] = rate
        listing["plot_negative_value_flag"] = False
    else:
        listing["plot_derived_rate_range"] = None
        listing["plot_derived_rate_per_sqft"] = None
        listing["plot_negative_value_flag"] = True


# ---------------------------------------------------------------------------
# Convenience wrapper — call this from your orchestration layer
# ---------------------------------------------------------------------------

def run_plot_rate_if_applicable(
    pipeline_output: Dict,
    subject: Dict,
    location: str,
    country: str,
    property_type: str,
    on_progress=None,
) -> Dict:
    """
    Thin wrapper that gates on property_type == 'plot'.

    Usage
    -----
    from data_cleaning import data_cleaning_pipeline
    from plot_rate_pipeline import run_plot_rate_if_applicable

    result = data_cleaning_pipeline(listings, subject, comparables, property_type)
    result = run_plot_rate_if_applicable(
        pipeline_output=result,
        subject=subject,
        location="Baner, Pune",
        country="India",
        property_type=property_type,
    )

    # cleaned_listings now have plot_derived_rate_per_sqft etc. when property_type=="plot"
    """
    return calculate_plot_rates(
        pipeline_output=pipeline_output,
        subject=subject,
        location=location,
        country=country,
        property_type=property_type,
        on_progress=on_progress,
    )