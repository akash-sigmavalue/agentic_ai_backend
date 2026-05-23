"""
plot_rate_pipeline.py
=====================
Post-pipeline step: enriches cleaned listings with Plot Area Rate data.

Call `calculate_plot_rates(...)` AFTER `data_cleaning_pipeline(...)` returns,
but ONLY when downstream valuation needs a plot/land rate. This includes
plot market valuation and villa cost approach valuation.

Each cleaned listing gets four new fields:
  - plot_fsi_range              : {"low": float, "high": float, "best": float}
  - plot_construction_cost_range: {"low": float, "high": float, "best": float, "currency": str}
  - plot_derived_rate_range     : {"low": float, "high": float, "currency": str}
  - plot_derived_rate_per_sqft  : float  (midpoint — the single headline number)
  - plot_derived_by             : str    ("llm" or "user")

Listings where price or area is null are skipped and get null for all four fields.

Model : gpt-4o-mini
"""

from typing import Callable
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

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

For EACH property in the input JSON array (whether it is a plot or a built-up property like a Villa, Apartment, or House), estimate typical FSI and Construction Cost per sqft to enable reverse-engineering of the Plot Area Rate or built-up rate.

INPUT per item:
  id             - identifier
  property_category - (e.g., Villa, Apartment, House, Plot)
  built_up_sqft  - super built-up area (null for plots)
  plot_area_sqft - raw land area (null for built-up properties)
  total_price    - property price (in local currency)
  location       - locality / city
  country        - country name

STEPS for EACH item:
  1. Identify Local Currency: Code (e.g. INR) and Symbol (e.g. ₹) from the country.
  2. Estimate FSI/FAR: Typical range (low/high) and best estimate for this property type and location. 
     You MUST estimate FSI even for built-up properties (villas, apartments), representing the typical allowable FSI for that property category in that locality.
     Briefly explain the reasoning (local norms, zone type).
  3. Estimate Construction Cost/sqft: Typical range (low/high) and best estimate in local currency.
     Factor in typical finish quality for the property type (e.g., villas have higher construction costs than standard apartments) and 2025-2026 market conditions.
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
- You MUST estimate realistic numeric values for FSI and construction cost for all items (plots, villas, apartments, etc.). Do NOT return null for these fields unless price or area is missing or <= 0.
- For built-up properties: if total_price or built_up_sqft is null/0, return null for all numeric fields.
- For plot properties: if total_price or plot_area_sqft is null/0, return null for all numeric fields.
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
    on_progress: Optional[Callable[[str, str], None]] = None,
    overrides: Optional[Dict] = None,
    fsi_override: Optional[float] = None,
    cc_override: Optional[float] = None,
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
        Subject property type. Call this only when the valuation rate basis is
        plot/land; e.g. plot market valuation or villa cost approach.
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
        on_progress("plot_rate_start", f"Starting plot/land rate calculation for {len(cleaned)} listings")
    print(f"🏗️  [Plot Rate] Starting for {len(cleaned)} cleaned listings …")

    # ── Split into processable vs. skippable ──────────────────────────────
    processable: List[Tuple[int, Dict]] = []   # (original index, listing)
    skipped_count = 0

    subject_needs_plot_land_rate = property_type.strip().lower() in ("plot", "villa")

    for orig_idx, lst in enumerate(cleaned):
        price = lst.get("cleaned_price_value")
        area  = lst.get("final_super_builtup_area")
        category_raw = lst.get("property_category") or lst.get("project_category") or lst.get("property_type") or ""
        ptype = str(category_raw).strip().lower()

        if not price or not area or float(price) <= 0 or float(area) <= 0:
            _stamp_null_plot_fields(cleaned[orig_idx])
            skipped_count += 1
            continue

        listing_is_plot = ptype in ["plot", "residential land", "land"]

        if subject_needs_plot_land_rate:
            if listing_is_plot:
                # Already a plot - use direct rate, skip LLM
                _stamp_direct_plot_fields(cleaned[orig_idx])
            else:
                # Apartment, Villa, etc. - queue for LLM reverse-engineering
                processable.append((orig_idx, lst))
        else:
            # Subject is built-up (e.g. Villa)
            if listing_is_plot:
                # Plot/Land - queue for LLM estimation (to calculate derived built-up rate)
                processable.append((orig_idx, lst))
            else:
                # Villa/Apartment - direct built-up rate (no plot-rate fields needed for valuation)
                _stamp_null_plot_fields(cleaned[orig_idx])
                skipped_count += 1

    print(f"   → {len(processable)} listings queued, {skipped_count} skipped (null price/area or already built-up)")

    # ── Handle Overrides (Skip LLM) ───────────────────────────────────────
    if overrides is not None:
        print("   ⚙️  Recalculating with user overrides (skipping LLM)")
        for seq_id, (orig_idx, lst) in enumerate(processable):
            ov = overrides.get(str(orig_idx)) or overrides.get(orig_idx) or {}
            
            # Helper to parse empty strings cleanly
            def parse_val(v):
                if v is None or str(v).strip() == "": return None
                try: return float(v)
                except ValueError: return None

            # 1. Determine FSI
            f_low  = parse_val(ov.get("fsi_low") or ov.get("fsi"))
            f_high = parse_val(ov.get("fsi_high") or ov.get("fsi"))
            
            if fsi_override is not None:
                if f_low is None: f_low = float(fsi_override)
                if f_high is None: f_high = float(fsi_override)
                
            if lst.get("plot_fsi_range"):
                if f_low is None: f_low = lst["plot_fsi_range"].get("low")
                if f_high is None: f_high = lst["plot_fsi_range"].get("high")

            f_low = f_low if f_low is not None else 1.0
            f_high = f_high if f_high is not None else 1.0
            f_best = (f_low + f_high) / 2.0  # Recalculate midpoint so headline rate changes
            
            # 2. Determine Construction Cost
            c_low  = parse_val(ov.get("cc_low") or ov.get("construction_cost"))
            c_high = parse_val(ov.get("cc_high") or ov.get("construction_cost"))
            
            if cc_override is not None:
                if c_low is None: c_low = float(cc_override)
                if c_high is None: c_high = float(cc_override)
                
            if lst.get("plot_construction_cost_range"):
                if c_low is None: c_low = lst["plot_construction_cost_range"].get("low")
                if c_high is None: c_high = lst["plot_construction_cost_range"].get("high")

            c_low = c_low if c_low is not None else 0.0
            c_high = c_high if c_high is not None else 0.0
            c_best = (c_low + c_high) / 2.0  # Recalculate midpoint

            # Determine if this row was actually overridden
            is_overridden = bool(ov) or (fsi_override is not None) or (cc_override is not None)
            row_derived_by = "user" if is_overridden else (lst.get("plot_derived_by") or "llm")

            mock_llm_result = {
                "fsi_low": f_low, "fsi_high": f_high, "fsi_best": f_best,
                "fsi_reasoning": "User override / Preserved",
                "const_cost_low": c_low, "const_cost_high": c_high, "const_cost_best": c_best,
                "const_cost_rationale": "User override / Preserved",
            }
            if lst.get("plot_construction_cost_range") and lst["plot_construction_cost_range"].get("currency"):
                mock_llm_result["currency_symbol"] = lst["plot_construction_cost_range"]["currency"]
            
            _stamp_plot_fields(cleaned[orig_idx], mock_llm_result, derived_by=row_derived_by)

        pipeline_output["audit_stats"]["plot_rate_token_usage"] = {"total_tokens": 0}
        pipeline_output["audit_stats"]["plot_rate_skipped"]     = skipped_count
        pipeline_output["audit_stats"]["plot_rate_processed"]   = len(processable)
        if on_progress:
            on_progress("plot_rate_done", "Plot rate calculation complete (using overrides)")
        print("✨ [Plot Rate] Done using user overrides.")
        return pipeline_output

    # Global Overrides (Conditional Skip LLM) is now handled inside the overrides block if overrides is not None.
    # If overrides is None but global overrides are provided, we should still handle them.
    if overrides is None and (fsi_override is not None or cc_override is not None):
        print(f"   ⚙️  Applying global overrides (FSI={fsi_override}, CC={cc_override})")
        for orig_idx, lst in processable:
            f_val = fsi_override
            c_val = cc_override
            
            # If only one is provided, keep the other from previous run if exists
            if f_val is None and lst.get("plot_fsi_range"):
                f_val = lst["plot_fsi_range"].get("best")
            if c_val is None and lst.get("plot_construction_cost_range"):
                c_val = lst["plot_construction_cost_range"].get("best")
                
            f_val = float(f_val) if f_val is not None else 1.0
            c_val = float(c_val) if c_val is not None else 0.0

            mock_res = {
                "fsi_low": f_val, "fsi_high": f_val, "fsi_best": f_val,
                "fsi_reasoning": "Global user override",
                "const_cost_low": c_val, "const_cost_high": c_val, "const_cost_best": c_val,
                "const_cost_rationale": "Global user override",
            }
            if lst.get("plot_construction_cost_range") and lst["plot_construction_cost_range"].get("currency"):
                mock_res["currency_symbol"] = lst["plot_construction_cost_range"]["currency"]
            
            _stamp_plot_fields(cleaned[orig_idx], mock_res, derived_by="user")

        pipeline_output["audit_stats"]["plot_rate_token_usage"] = {"total_tokens": 0}
        pipeline_output["audit_stats"]["plot_rate_processed"]   = len(processable)
        if on_progress:
            on_progress("plot_rate_done", "Plot rate calculation complete (global overrides)")
        return pipeline_output

    # ── Build LLM input items ─────────────────────────────────────────────
    llm_items: List[Dict] = []
    for seq_id, (orig_idx, lst) in enumerate(processable):
        price = lst.get("cleaned_price_value")
        area  = lst.get("final_super_builtup_area")
        category_raw = lst.get("property_category") or lst.get("project_category") or lst.get("property_type") or ""
        item_ptype = str(category_raw).strip().lower()
        item_is_plot = item_ptype in ["plot", "residential land", "land"] 

        llm_items.append({
            "id":            seq_id,
            "property_category": item_ptype,
            "project_name":  lst.get("project_name", "Unknown"),
            "total_price":   price,
            "plot_area_sqft": area if item_is_plot else None,  
            "built_up_sqft":  None if item_is_plot else area,  
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
            _stamp_plot_fields(cleaned[orig_idx], result, derived_by="llm")
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

def _stamp_plot_fields(listing: Dict, result: Dict, derived_by: str = "llm") -> None:
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
    category_raw = listing.get("property_category") or listing.get("project_category") or listing.get("property_type") or ""
    listing_is_plot = str(category_raw).strip().lower() in ["plot", "residential land", "land"]

    if listing_is_plot:
        # Calculating Derived Built-up Rate for a Plot comparable
        price_num = float(price or 0)
        area_num = float(area or 0)
        if area_num > 0 and (fsi_low or 0) > 0 and (fsi_high or 0) > 0 and (fsi_best or 0) > 0:
            land_rate = price_num / area_num
            rate_low = (land_rate / float(fsi_high)) + float(cost_low or 0)
            rate_high = (land_rate / float(fsi_low)) + float(cost_high or 0)
            rate_best = (land_rate / float(fsi_best)) + float(cost_best or 0)
            
            calc = {
                "rate_low": round(rate_low, 2),
                "rate_high": round(rate_high, 2),
                "rate_best": round(rate_best, 2),
                "negative_value": False
            }
        else:
            calc = {"negative_value": True}
    else:
        # Perform Deterministic Calculation (Residual Land Rate for Built-up comparable)
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
        listing["plot_derived_by"] = derived_by
    else:
        listing["plot_derived_rate_range"] = {
            "low": calc.get("rate_low"),
            "high": calc.get("rate_high"),
            "currency": currency,
        }
        listing["plot_derived_rate_per_sqft"] = calc.get("rate_best")
        listing["plot_negative_value_flag"] = False
        listing["plot_derived_by"] = derived_by

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
    listing["plot_derived_by"]              = None


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
        listing["plot_derived_by"] = "system"
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
