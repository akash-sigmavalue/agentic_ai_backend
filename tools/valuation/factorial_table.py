"""
Factorial Rate Table — computes per-project rate statistics from cleaned listings.

After data-cleaning produces normalised rows with `cleaned_price_value_inr` and
`final_super_builtup_area`, this module groups them by `cleaned_match_project`
and calculates:
    • Average rate  (price / area)
    • Median rate
    • 90th-percentile rate

The output is a compact list ready for the UI table and downstream valuation math.
"""

import logging
import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional
from tools.valuation.valuation_stats import calculate_project_ci
from tools.valuation.amenity_analytics_tool import get_amenity_counts

logger = logging.getLogger("factorial_table")


def compute_factorial_table(
    cleaned_listings: List[Dict],
    subject: Dict,
    comparables: List[Dict],
    currency: str = "INR",
    area_unit: str = "sqft",
    rate_basis: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the factorial rate summary.

    MICROMARKET FALLBACK (non-plot types only):
      If the subject project has zero listings, derive its rate from
      the average of all comparable projects.  The 90 % CI is set to
      ±5 % of that average.  A ``rate_derived_from`` field is stamped
      on every row: "listing" (real data) or "micromarket" (derived).
    """
    subject_name = subject.get("project_name", "Subject Property")
    property_type = subject.get("property_type", "apartment")
    rate_basis = _resolve_rate_basis(property_type, subject, rate_basis)

    # Property types eligible for micromarket fallback (not plots)
    MICROMARKET_ELIGIBLE = {"apartment", "flat", "villa", "retail", "shop",
                            "commercial_office", "office", "mixed_use"}
    
    # Map project names to coordinates for dynamic tool lookups
    coord_map = {}
    
    # Subject coords
    s_lat = subject.get("lat") or subject.get("map_search_lat")
    s_lng = subject.get("lng") or subject.get("map_search_lng")
    if subject_name and s_lat and s_lng:
        coord_map[subject_name.lower()] = (float(s_lat), float(s_lng), subject.get("location_name") or subject.get("location", ""))
        # Also map the location name to these coordinates (for general search benchmarks)
        loc_name = subject.get("location_name") or subject.get("locality")
        if loc_name:
            coord_map[loc_name.lower()] = (float(s_lat), float(s_lng), loc_name)
        
    # Comparable coords
    for c in comparables:
        cname = c.get("project_name")
        c_lat = c.get("lat") or c.get("map_search_lat")
        c_lng = c.get("lng") or c.get("map_search_lng")
        if cname and c_lat and c_lng:
            coord_map[cname.lower()] = (float(c_lat), float(c_lng), c.get("location") or c.get("location_name", ""))

    if not cleaned_listings:
        return {
            "table": [],
            "currency": currency,
            "area_unit": area_unit,
            "rate_basis": rate_basis,
            "total_valid": 0,
        }

    # ---- Single LLM call: identify CBDs for ALL projects at once ---------
    cbd_map: Dict[str, list] = {}
    try:
        from tools.valuation.cbd_identification_tool import identify_cbds
        cbd_map = identify_cbds(subject=subject, comparables=comparables)
        logger.info(f"CBD identification complete for {len(cbd_map)} projects")
    except Exception as e:
        logger.error(f"CBD identification failed: {e}")

    df = pd.DataFrame(cleaned_listings)

    # --- Ensure required columns exist -----------------------------------
    price_col = "cleaned_price_value"
    area_col = "final_super_builtup_area"
    project_col = "cleaned_match_project"

    for col in [price_col, area_col, project_col]:
        if col not in df.columns:
            logger.warning(f"Missing column {col} — returning empty table")
            return {
                "table": [],
                "currency": currency,
                "area_unit": area_unit,
                "rate_basis": rate_basis,
                "total_valid": 0,
            }

    # --- Filter only rows with valid price & area ------------------------
    df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
    df[area_col] = pd.to_numeric(df[area_col], errors="coerce")

    valid = df[df[price_col].notna() & df[area_col].notna() & (df[area_col] > 0)].copy()

    if valid.empty:
        return {
            "table": [],
            "currency": currency,
            "area_unit": area_unit,
            "rate_basis": rate_basis,
            "total_valid": 0,
        }

    # --- Calculate Rate per listing --------------------------------------
    if rate_basis == "plot_land":
        if "plot_derived_rate_per_sqft" not in valid.columns:
            logger.warning("Plot-land rate basis requested but plot_derived_rate_per_sqft is missing.")
            return {
                "table": [],
                "currency": currency,
                "area_unit": area_unit,
                "rate_basis": rate_basis,
                "total_valid": 0,
            }
        valid["rate"] = pd.to_numeric(valid["plot_derived_rate_per_sqft"], errors="coerce")
        valid = valid[valid["rate"].notna() & (valid["rate"] > 0)].copy()
    else:
        valid["rate"] = valid[price_col] / valid[area_col]

    if valid.empty:
        return {
            "table": [],
            "currency": currency,
            "area_unit": area_unit,
            "rate_basis": rate_basis,
            "total_valid": 0,
        }

    # --- Group by project ------------------------------------------------
    summary_rows: List[Dict] = []

    for project, grp in valid.groupby(project_col, dropna=False):
        if pd.isna(project):
            continue

        rates = grp["rate"].dropna()
        if rates.empty:
            continue

        avg_rate = float(rates.mean())
        median_rate = float(rates.median())
        p90_rate = float(np.percentile(rates, 90))

        # CI calculation using robust stats engine (Student's T-distribution)
        ci_90_lower, ci_90_upper, _ = calculate_project_ci(rates.tolist(), confidence_level=0.90)

        # Initialize metrics
        road_type = None
        amenities = []
        amenity_summary = {"total": 0, "counts": get_amenity_counts([])}
        builtup_density = None
        cbd_data = []

        # Look up coordinates for this project
        lat, lng, loc = None, None, ""
        for k, v in coord_map.items():
            if _fuzzy_match(str(project), k):
                lat, lng, loc = v
                break

        if lat and lng:
            from tools.valuation.road_infrastructure_tool import get_road_category
            from tools.valuation.amenity_analytics_tool import get_nearby_amenities
            from tools.valuation.builtup_density_tool import analyze_congestion
            
            try:
                road_type = get_road_category(lat, lng)
            except Exception as e:
                logger.error(f"Road fetch failed for {project}: {e}")
                
            try:
                amenities = get_nearby_amenities(lat, lng, city_name=loc)
                amenity_summary = {
                    "total": len(amenities),
                    "counts": get_amenity_counts(amenities)
                }
            except Exception as e:
                logger.error(f"Amenity fetch failed for {project}: {e}")

            try:
                builtup_density = analyze_congestion(lat, lng, 500)
            except Exception as e:
                logger.error(f"Failed to fetch builtup density for {project}: {e}")
        else:
            # Fallback to listing data if no coordinates match
            if "road_type" in grp.columns:
                road_types = grp["road_type"].dropna()
                if not road_types.empty:
                    road_type = str(road_types.iloc[0])
            
            if "amenities" in grp.columns:
                ams = grp["amenities"].dropna()
                if not ams.empty:
                    amenities = ams.iloc[0]
            
            if "amenity_summary" in grp.columns:
                sums = grp["amenity_summary"].dropna()
                if not sums.empty:
                    amenity_summary = sums.iloc[0]

        # Look up CBD data for this project
        for cbd_key, cbd_list in cbd_map.items():
            if _fuzzy_match(str(project), cbd_key):
                cbd_data = cbd_list
                break

        # We no longer calculate CBD score; we just pass the cbd_data to the frontend.

        is_subject = _fuzzy_match(str(project), subject_name)

        row_data = {
            "project_name": str(project),
            "is_subject": is_subject,
            "listing_count": len(rates),
            "avg_rate": round(avg_rate, 2),
            "median_rate": round(median_rate, 2),
            "p90_rate": round(p90_rate, 2),
            "ci_90_lower": ci_90_lower,
            "ci_90_upper": ci_90_upper,
            "rate_derived_from": "listing",
            "road_type": road_type,
            "amenities": amenities,
            "amenity_summary": amenity_summary,
            "cbd_data": cbd_data,
            "builtup_density": builtup_density,
        }
        summary_rows.append(row_data)

    # ── Micromarket fallback: derive subject rate from comparables ────────
    # Only for non-plot property types (apartment, villa, shop, office, etc.)
    subject_found = any(r["is_subject"] for r in summary_rows)
    ptype_lower = property_type.lower().strip()

    if not subject_found and ptype_lower in MICROMARKET_ELIGIBLE:
        # Collect rates from all comparable rows
        comp_rates = [r["avg_rate"] for r in summary_rows if not r["is_subject"] and r["avg_rate"] > 0]

        if comp_rates:
            micromarket_avg = sum(comp_rates) / len(comp_rates)
            ci_lower = round(micromarket_avg * 0.95, 2)  # −5 %
            ci_upper = round(micromarket_avg * 1.05, 2)  # +5 %

            logger.info(
                f"[Micromarket Fallback] Subject '{subject_name}' has no listings. "
                f"Deriving rate from {len(comp_rates)} comparable(s): "
                f"avg={round(micromarket_avg, 2)}, CI=[{ci_lower}, {ci_upper}]"
            )

            # Build subject metrics (road, amenity, CBD, density)
            subj_road_type = None
            subj_amenities = []
            subj_amenity_summary = {"total": 0, "counts": get_amenity_counts([])}
            subj_builtup_density = None
            subj_cbd_data = []

            if s_lat and s_lng:
                from tools.valuation.road_infrastructure_tool import get_road_category
                from tools.valuation.amenity_analytics_tool import get_nearby_amenities
                from tools.valuation.builtup_density_tool import analyze_congestion

                try:
                    subj_road_type = get_road_category(float(s_lat), float(s_lng))
                except Exception as e:
                    logger.error(f"Road fetch failed for subject '{subject_name}': {e}")

                subj_loc = subject.get("location_name") or subject.get("location", "")
                try:
                    subj_amenities = get_nearby_amenities(float(s_lat), float(s_lng), city_name=subj_loc)
                    subj_amenity_summary = {
                        "total": len(subj_amenities),
                        "counts": get_amenity_counts(subj_amenities),
                    }
                except Exception as e:
                    logger.error(f"Amenity fetch failed for subject '{subject_name}': {e}")

                try:
                    subj_builtup_density = analyze_congestion(float(s_lat), float(s_lng), 500)
                except Exception as e:
                    logger.error(f"Builtup density failed for subject '{subject_name}': {e}")

            # CBD data for subject
            for cbd_key, cbd_list in cbd_map.items():
                if _fuzzy_match(subject_name, cbd_key):
                    subj_cbd_data = cbd_list
                    break

            summary_rows.insert(0, {
                "project_name": subject_name,
                "is_subject": True,
                "listing_count": 0,
                "avg_rate": round(micromarket_avg, 2),
                "median_rate": round(micromarket_avg, 2),
                "p90_rate": round(micromarket_avg, 2),
                "ci_90_lower": ci_lower,
                "ci_90_upper": ci_upper,
                "rate_derived_from": "micromarket",
                "road_type": subj_road_type,
                "amenities": subj_amenities,
                "amenity_summary": subj_amenity_summary,
                "cbd_data": subj_cbd_data,
                "builtup_density": subj_builtup_density,
            })
        else:
            logger.warning(
                f"[Micromarket Fallback] Subject '{subject_name}' has no listings "
                f"and no comparable rates available — cannot derive rate"
            )

    # Sort: subject first, then by listing_count descending
    summary_rows.sort(key=lambda r: (not r["is_subject"], -r["listing_count"]))

    logger.info(
        f"Factorial table: {len(summary_rows)} projects, "
        f"{int(valid.shape[0])} total valid listings"
    )

    # Identify primary area type from valid listings
    area_type = "Built-up Area"
    if rate_basis == "plot_land":
        area_type = "Plot Land Area"
    elif "cleaned_area_type" in valid.columns:
        mode_series = valid["cleaned_area_type"].mode()
        if not mode_series.empty:
            area_type = str(mode_series[0]).replace("_", " ").title()

    return {
        "table": summary_rows,
        "currency": currency,
        "area_unit": area_unit,
        "area_type": area_type,
        "rate_basis": rate_basis,
        "total_valid": int(valid.shape[0]),
    }


def _resolve_rate_basis(property_type: str, subject: Dict, requested: Optional[str]) -> str:
    requested_normalized = (requested or "").strip().lower()
    if requested_normalized in {"plot_land", "built_up"}:
        return requested_normalized

    ptype = (property_type or "").strip().lower()
    approach = (
        subject.get("recommended_approach")
        or subject.get("user_requested_approach")
        or subject.get("approach")
        or ""
    )
    approach = str(approach).strip().lower()

    if ptype == "plot":
        return "plot_land"
    if ptype == "villa" and approach == "cost":
        return "plot_land"
    return "built_up"


def _fuzzy_match(a: str, b: str) -> bool:
    """Case-insensitive substring check in both directions."""
    a_lower = a.strip().lower()
    b_lower = b.strip().lower()
    return a_lower in b_lower or b_lower in a_lower
