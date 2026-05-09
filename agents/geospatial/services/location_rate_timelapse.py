"""
location_rate_timelapse.py
Builds the Location Rate Heatmap + Transaction Volume Pulse timelapse response.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# from api.schemas.geospatial.maps import LocationMonthValue


def _safe_float(val, default=None):
    """Safely convert a value to float, handling pd.NA / NAType and other edge cases."""
    if val is None:
        return default
    try:
        # Check for pandas NAType explicitly
        if hasattr(val, "__class__") and val.__class__.__name__ == "NAType":
            return default
        if pd.isna(val):
            return default
        return float(val)
    except (TypeError, ValueError, AttributeError):
        return default

# from agents.geospatial.services.timelapse_preprocessing import get_timelapse_data_path
from api.schemas.geospatial.maps import (
    LocationMonthValue,
    LocationRateTimelapseRequest,
    LocationRateTimelapseResponse,
    LocationTimelapse,
)
from agents.geospatial.services.timelapse_preprocessing import get_timelapse_data_path, load_and_preprocess

_DEFAULT_CENTER = [18.5204, 73.8567]


def _empty_response(warnings: list[str], msg: str = "") -> LocationRateTimelapseResponse:
    if msg:
        warnings.append(msg)
    return LocationRateTimelapseResponse(
        map_center=_DEFAULT_CENTER,
        timeline=[],
        locations=[],
        warnings=warnings,
        available_locations=[],
        available_micro_markets=[],
        global_min_rate=0.0,
        global_max_rate=1.0,
    )


def _normalize_series(s: pd.Series) -> pd.Series:
    mn, mx = s.min(), s.max()
    if mx == mn:
        return pd.Series(0.0, index=s.index)
    return (s - mn) / (mx - mn)


def build_location_rate_timelapse(
    request: LocationRateTimelapseRequest,
) -> LocationRateTimelapseResponse:
    warnings_list: list[str] = []

    excel_path = get_timelapse_data_path()
    df = load_and_preprocess(excel_path, warnings_list)

    if df.empty:
        return _empty_response(warnings_list)

    df = df[df["valid_transaction_flag"]].copy()

    # ── Search / Filter ───────────────────────────────────────────────────────
    search = (request.search or "").strip().lower()
    if search:
        cols = ["location_name", "micro_market", "village_name", "city_name", "project_name"]
        
        full_mask = pd.Series(False, index=df.index)
        for col in cols:
            if col in df.columns:
                full_mask |= df[col].astype(str).str.lower().str.contains(search, na=False)
                
        if full_mask.sum() > 0:
            df = df[full_mask]
        else:
            parts = [p.strip() for p in search.split(",") if p.strip()]
            parts = [p for p in parts if len(p) > 3]
            
            found_match = False
            for part in parts:
                part_mask = pd.Series(False, index=df.index)
                for col in cols:
                    if col in df.columns:
                        part_mask |= df[col].astype(str).str.lower().str.contains(part, na=False)
                
                if part_mask.sum() > 0:
                    df = df[part_mask]
                    found_match = True
                    break
            
            if not found_match:
                df = df.iloc[0:0]

    def filter_col(col: str, val: Optional[str]) -> None:
        nonlocal df
        if val and col in df.columns:
            df = df[df[col].astype(str).str.lower().str.contains(val.lower(), na=False)]

    filter_col("location_name",     request.location_name)
    filter_col("micro_market",      request.micro_market)
    filter_col("property_type",     request.property_type)
    filter_col("unit_configuration",request.unit_configuration)
    filter_col("sale_type",         request.sale_type)

    if request.start_month:
        df = df[df["txn_month"] >= request.start_month]
    if request.end_month:
        df = df[df["txn_month"] <= request.end_month]

    if df.empty:
        return _empty_response(warnings_list, "No transactions matched the given filters.")

    available_locations     = sorted(df["location_name"].dropna().unique().tolist()) if "location_name" in df.columns else []
    available_micro_markets = sorted(df["micro_market"].dropna().unique().tolist())  if "micro_market"  in df.columns else []

    df = df.dropna(subset=["rate_psf"])

    if df.empty:
        return _empty_response(warnings_list, "No rows with valid rate_psf after filtering.")

    # ── Decide grouping key ───────────────────────────────────────────────────
    has_location    = "location_name"  in df.columns
    has_micro       = "micro_market"   in df.columns
    has_city        = "city_name"      in df.columns

    if has_micro and has_location:
        geo_cols = ["micro_market", "location_name"]
    elif has_location:
        geo_cols = ["location_name"]
    elif has_city:
        geo_cols = ["city_name"]
    else:
        return _empty_response(warnings_list, "Dataset has no location_name, micro_market or city_name column.")

    group_cols = geo_cols + ["txn_month"]

    # ── Transaction volume: count distinct document_number or rows ────────────
    has_doc = "document_number" in df.columns

    agg_dict: dict = {
        "median_rate_psf":       ("rate_psf",        "median"),
        "avg_rate_psf":          ("rate_psf",        "mean"),
        "total_agreement_value": ("agreement_price", "sum"),
    }
    if has_doc:
        agg_dict["transaction_volume"] = ("document_number", "nunique")
    else:
        agg_dict["transaction_volume"] = ("rate_psf", "count")

    if "project_id" in df.columns:
        agg_dict["active_project_count"] = ("project_id", "nunique")
    elif "project_name" in df.columns:
        agg_dict["active_project_count"] = ("project_name", "nunique")

    if "agreement_price" not in df.columns:
        df["agreement_price"] = np.nan

    agg = df.groupby(group_cols, as_index=False, dropna=False).agg(**{k: v for k, v in agg_dict.items()})
    # Replace pd.NA with np.nan to avoid float() issues later
    agg = agg.replace({pd.NA: np.nan})

    # ── MoM growth ────────────────────────────────────────────────────────────
    lag_group = geo_cols
    agg = agg.sort_values(lag_group + ["txn_month"])

    def _mom_pct(col: str) -> pd.Series:
        prev = agg.groupby(lag_group, dropna=False)[col].shift(1)
        valid = prev.notna() & (prev > 0)
        return pd.Series(
            np.where(valid, ((agg[col] - prev) / prev) * 100, np.nan),
            index=agg.index,
        )

    agg["rate_growth_pct"]   = _mom_pct("median_rate_psf")
    agg["volume_growth_pct"] = _mom_pct("transaction_volume")

    # ── Momentum score ────────────────────────────────────────────────────────
    r_norm = _normalize_series(agg["rate_growth_pct"].fillna(0))
    v_norm = _normalize_series(agg["volume_growth_pct"].fillna(0))
    agg["momentum_score"] = (0.6 * r_norm + 0.4 * v_norm) * 100

    timeline = sorted(agg["txn_month"].unique().tolist())
    all_rates = [r for r in agg["median_rate_psf"].tolist() if not pd.isna(r)]
    global_min = _safe_float(np.min(all_rates), 0.0) if all_rates else 0.0
    global_max = _safe_float(np.max(all_rates), 1.0) if all_rates else 1.0

    # ── Coordinates per location ──────────────────────────────────────────────
    loc_key = geo_cols[0]      # primary grouping col for coords

    # Prefer location lat/lng; fall back to project lat/lng
    coord_cols_loc  = ["location_latitude",  "location_longitude"]
    coord_cols_proj = ["project_latitude",   "project_longitude"]

    def _coord_agg(lat_col: str, lng_col: str) -> dict[str, tuple[float, float]]:
        result: dict[str, tuple[float, float]] = {}
        if lat_col not in df.columns or lng_col not in df.columns:
            return result
        sub = df.groupby(loc_key, as_index=False, dropna=False).agg(
            lat=(lat_col, "mean"),
            lng=(lng_col, "mean"),
        )
        for _, r in sub.iterrows():
            if pd.notna(r["lat"]) and pd.notna(r["lng"]):
                result[str(r[loc_key])] = (_safe_float(r["lat"]), _safe_float(r["lng"]))
        return result

    coord_map = _coord_agg(*coord_cols_loc)
    proj_map  = _coord_agg(*coord_cols_proj)
    # Merge: use location coords where available, else project coords
    for k, v in proj_map.items():
        if k not in coord_map:
            coord_map[k] = v

    # ── Build locations ───────────────────────────────────────────────────────
    locations_out: list[LocationTimelapse] = []

    for keys, grp in agg.groupby(geo_cols, dropna=False):
        if isinstance(keys, str):
            loc_name   = keys
            micro_mkt  = None
        else:
            loc_name  = keys[1] if len(keys) > 1 else keys[0]
            micro_mkt = keys[0] if len(keys) > 1 else None

        if pd.isna(loc_name):
            continue
        if pd.isna(micro_mkt):
            micro_mkt = None

        lookup_key = str(keys[0]) if isinstance(keys, tuple) else str(keys)
        if lookup_key not in coord_map:
            warnings_list.append(f"Skipping '{lookup_key}': no valid coordinates.")
            continue

        lat, lng = coord_map[lookup_key]

        monthly_values: dict[str, LocationMonthValue] = {}
        for _, row in grp.iterrows():
            mon = str(row["txn_month"])
            monthly_values[mon] = LocationMonthValue(
                median_rate_psf     = _safe_float(row["median_rate_psf"]),
                avg_rate_psf        = _safe_float(row["avg_rate_psf"]),
                transaction_volume  = int(row["transaction_volume"])    if pd.notna(row.get("transaction_volume"))  else 0,
                active_project_count= int(row.get("active_project_count", 0)) if pd.notna(row.get("active_project_count", 0)) else 0,
                total_agreement_value= _safe_float(row["total_agreement_value"], 0.0),
                rate_growth_pct     = _safe_float(row.get("rate_growth_pct")),
                volume_growth_pct   = _safe_float(row.get("volume_growth_pct")),
                momentum_score      = _safe_float(row.get("momentum_score")),
            )

        locations_out.append(LocationTimelapse(
            location_name  = str(loc_name),
            micro_market   = str(micro_mkt) if micro_mkt else None,
            latitude       = lat,
            longitude      = lng,
            monthly_values = monthly_values,
        ))

    if not locations_out:
        return _empty_response(warnings_list, "No locations with valid coordinates could be rendered.")

    lats = [lo.latitude for lo in locations_out if not pd.isna(lo.latitude)]
    lngs = [lo.longitude for lo in locations_out if not pd.isna(lo.longitude)]
    center_lat = _safe_float(np.mean(lats), _DEFAULT_CENTER[0]) if lats else _DEFAULT_CENTER[0]
    center_lng = _safe_float(np.mean(lngs), _DEFAULT_CENTER[1]) if lngs else _DEFAULT_CENTER[1]

    return LocationRateTimelapseResponse(
        map_center             = [center_lat, center_lng],
        timeline               = timeline,
        locations              = locations_out,
        warnings               = warnings_list,
        available_locations    = available_locations[:200],
        available_micro_markets= available_micro_markets[:200],
        global_min_rate        = global_min,
        global_max_rate        = global_max,
    )
