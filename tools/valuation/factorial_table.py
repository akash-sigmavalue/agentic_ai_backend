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
    sorted_comparables = sorted(
        comparables,
        key=lambda c: 0 if str(c.get("data_source") or c.get("source")).strip().lower() == "internal db" else 1
    )
    for c in sorted_comparables:
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
        is_subject_villa = property_type.strip().lower() == "villa"
        
        def calculate_built_up_rate(row):
            cat = str(row.get("property_category", "")).strip().lower()
            if not cat or cat == "nan":
                cat = str(row.get("project_category", "")).strip().lower()
            if not cat or cat == "nan":
                cat = str(row.get("property_type", "")).strip().lower()
                
            is_plot = cat in ["plot", "residential land", "land"]
            if is_subject_villa and is_plot:
                val = row.get("plot_derived_rate_per_sqft")
                try:
                    return float(val) if pd.notna(val) else np.nan
                except (ValueError, TypeError):
                    return np.nan
            else:
                return row[price_col] / row[area_col]

        valid["rate"] = valid.apply(calculate_built_up_rate, axis=1)
        valid = valid[valid["rate"].notna() & (valid["rate"] > 0)].copy()

    # Ensure source column exists in valid dataframe
    if "source" not in valid.columns:
        valid["source"] = "Web"

    # --- Group by project and source to calculate raw sub-groups ---------
    raw_groups = []
    for (project, src), grp in valid.groupby([project_col, "source"], dropna=False):
        if pd.isna(project):
            continue
        rates = grp["rate"].dropna()
        if rates.empty:
            continue
        raw_groups.append({
            "project_name": str(project),
            "source": str(src),
            "rates": rates.tolist(),
            "grp": grp,
        })

    # Helpers for coordinates and distance matching
    def get_project_coords(project_name):
        for k, v in coord_map.items():
            if _fuzzy_match(project_name, k):
                return v[0], v[1]
        return None, None

    import math
    def haversine_dist(lat1, lon1, lat2, lon2):
        R = 6371.0  # Earth radius in km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c * 1000.0  # distance in meters

    def is_same_physical_project(p1_name, p2_name):
        if not _fuzzy_match(p1_name, p2_name):
            return False
        lat1, lng1 = get_project_coords(p1_name)
        lat2, lng2 = get_project_coords(p2_name)
        if lat1 is not None and lat2 is not None:
            return haversine_dist(lat1, lng1, lat2, lng2) <= 100.0
        # Fallback if coordinates are missing: only combine if exact match (ignoring case/whitespace)
        return p1_name.strip().lower() == p2_name.strip().lower()

    # --- Merge similar physical projects ---
    merged_projects = []
    for rg in raw_groups:
        matched_parent = None
        for parent in merged_projects:
            if is_same_physical_project(rg["project_name"], parent["project_name"]):
                matched_parent = parent
                break
        if matched_parent:
            matched_parent["sub_groups"].append(rg)
        else:
            merged_projects.append({
                "project_name": rg["project_name"],
                "sub_groups": [rg]
            })

    # --- Pre-fetch geospatial metrics concurrently ---
    from concurrent.futures import ThreadPoolExecutor
    
    # Identify coordinates for each parent project
    project_coords = {}
    for parent in merged_projects:
        pname = parent["project_name"]
        lat, lng, loc = None, None, ""
        for k, v in coord_map.items():
            if _fuzzy_match(pname, k):
                lat, lng, loc = v
                break
        if lat and lng:
            project_coords[pname] = (lat, lng, loc)

    def fetch_geospatial_metrics(pname, lat, lng, loc):
        from tools.valuation.road_infrastructure_tool import get_road_category
        from tools.valuation.amenity_analytics_tool import get_nearby_amenities
        from tools.valuation.builtup_density_tool import analyze_congestion
        
        road_type = None
        amenities = []
        builtup_density = None
        
        try:
            road_type = get_road_category(lat, lng)
        except Exception as e:
            logger.error(f"Road fetch failed for {pname}: {e}")
            
        try:
            amenities = get_nearby_amenities(lat, lng, city_name=loc)
        except Exception as e:
            logger.error(f"Amenity fetch failed for {pname}: {e}")
            
        try:
            builtup_density = analyze_congestion(lat, lng, 500)
        except Exception as e:
            logger.error(f"Failed to fetch builtup density for {pname}: {e}")
            
        return pname, road_type, amenities, builtup_density

    geospatial_results = {}
    if project_coords:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(fetch_geospatial_metrics, pname, lat, lng, loc)
                for pname, (lat, lng, loc) in project_coords.items()
            ]
            for fut in futures:
                try:
                    pname, r_type, ams, density = fut.result()
                    geospatial_results[pname] = {
                        "road_type": r_type,
                        "amenities": ams,
                        "builtup_density": density
                    }
                except Exception as e:
                    logger.error(f"Error fetching geospatial metrics: {e}")

    # --- Build final summary rows with nested sub_rows ---
    summary_rows: List[Dict] = []
    for parent in merged_projects:
        pname = parent["project_name"]
        sub_groups = parent["sub_groups"]

        # Aggregate rates for parent calculations
        all_rates = []
        for sg in sub_groups:
            all_rates.extend(sg["rates"])

        all_rates_series = pd.Series(all_rates)
        avg_rate = float(all_rates_series.mean())
        median_rate = float(all_rates_series.median())
        p90_rate = float(np.percentile(all_rates, 90))
        ci_90_lower, ci_90_upper, _ = calculate_project_ci(all_rates, confidence_level=0.90)

        # Subject status
        is_subject = any(_fuzzy_match(sg["project_name"], subject_name) for sg in sub_groups)

        # Initialize metrics for parent
        road_type = None
        amenities = []
        amenity_summary = {"total": 0, "counts": get_amenity_counts([])}
        builtup_density = None
        cbd_data = []

        if pname in geospatial_results:
            res = geospatial_results[pname]
            road_type = res["road_type"]
            amenities = res["amenities"]
            amenity_summary = {
                "total": len(amenities),
                "counts": get_amenity_counts(amenities)
            }
            builtup_density = res["builtup_density"]
        else:
            # Fallback to the first sub-group's dataframe columns
            first_grp = sub_groups[0]["grp"]
            if "road_type" in first_grp.columns:
                road_types = first_grp["road_type"].dropna()
                if not road_types.empty:
                    road_type = str(road_types.iloc[0])
            
            if "amenities" in first_grp.columns:
                ams = first_grp["amenities"].dropna()
                if not ams.empty:
                    amenities = ams.iloc[0]
            
            if "amenity_summary" in first_grp.columns:
                sums = first_grp["amenity_summary"].dropna()
                if not sums.empty:
                    amenity_summary = sums.iloc[0]

        # Look up CBD data
        for cbd_key, cbd_list in cbd_map.items():
            if _fuzzy_match(pname, cbd_key):
                cbd_data = cbd_list
                break

        # Determine overall rate_derived_from
        sources_present = {sg["source"] for sg in sub_groups}
        has_db = any(s.strip().lower() == "internal db" for s in sources_present)
        has_web = any(s.strip().lower() == "web" for s in sources_present)

        if has_db and has_web:
            rate_derived_from = "mixed"
        elif has_db:
            rate_derived_from = "internal_db"
        else:
            rate_derived_from = "listing"

        # Build sub_rows representing individual sources
        sub_rows_data = []
        for sg in sub_groups:
            sg_rates = pd.Series(sg["rates"])
            sg_avg = float(sg_rates.mean())
            sg_median = float(sg_rates.median())
            sg_p90 = float(np.percentile(sg["rates"], 90))
            sg_ci_lower, sg_ci_upper, _ = calculate_project_ci(sg["rates"], confidence_level=0.90)

            is_sg_db = sg["source"].strip().lower() == "internal db"

            sub_rows_data.append({
                "project_name": sg["project_name"],
                "listing_count": len(sg["rates"]),
                "avg_rate": round(sg_avg, 2),
                "median_rate": round(sg_median, 2),
                "p90_rate": round(sg_p90, 2),
                "ci_90_lower": sg_ci_lower,
                "ci_90_upper": sg_ci_upper,
                "rate_derived_from": "internal_db" if is_sg_db else "listing",
            })

        row_data = {
            "project_name": str(pname),
            "is_subject": is_subject,
            "listing_count": len(all_rates),
            "avg_rate": round(avg_rate, 2),
            "median_rate": round(median_rate, 2),
            "p90_rate": round(p90_rate, 2),
            "ci_90_lower": ci_90_lower,
            "ci_90_upper": ci_90_upper,
            "rate_derived_from": rate_derived_from,
            "road_type": road_type,
            "amenities": amenities,
            "amenity_summary": amenity_summary,
            "cbd_data": cbd_data,
            "builtup_density": builtup_density,
            "sub_rows": sub_rows_data,
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
                "sub_rows": [],
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
