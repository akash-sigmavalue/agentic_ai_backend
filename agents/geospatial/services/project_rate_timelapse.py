"""
project_rate_timelapse.py
Builds the Project Rate + Growth Velocity timelapse response.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd


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

from api.schemas.geospatial.maps import (
    BuildingTimelapse,
    FloorMonthValue,
    FloorTimelapse,
    ProjectRateTimelapseRequest,
    ProjectRateTimelapseResponse,
)
from agents.geospatial.services.timelapse_preprocessing import get_timelapse_data_path, load_and_preprocess

_DEFAULT_CENTER = [18.5204, 73.8567]


def _empty_response(warnings: list[str], msg: str = "") -> ProjectRateTimelapseResponse:
    if msg:
        warnings.append(msg)
    return ProjectRateTimelapseResponse(
        map_center=_DEFAULT_CENTER,
        timeline=[],
        buildings=[],
        warnings=warnings,
        available_projects=[],
        available_towers=[],
        global_min_rate=0.0,
        global_max_rate=1.0,
    )


def _month_dist(a: str, b: str) -> int:
    """Simple numeric distance between 'YYYY-MM' strings."""
    try:
        ay, am = map(int, a.split("-"))
        by, bm = map(int, b.split("-"))
        return abs((ay * 12 + am) - (by * 12 + bm))
    except Exception:
        return 9999


def _apply_fallback(
    floor_data: dict[str, FloorMonthValue],
    timeline: list[str],
    all_floor_data: dict[int, dict[str, FloorMonthValue]],
    floor_idx: int,
) -> None:
    """Fill missing months with fallback levels 2-7."""
    for month in timeline:
        if month in floor_data:
            continue

        # Level 2: same floor, nearby month ±2
        nearby = [(m, v) for m, v in floor_data.items() if _month_dist(m, month) <= 2]
        if nearby:
            closest_m, closest_v = min(nearby, key=lambda x: _month_dist(x[0], month))
            d = _month_dist(closest_m, month)
            conf = 0.85 if d <= 1 else 0.70
            floor_data[month] = FloorMonthValue(
                rate_psf=closest_v.rate_psf,
                mom_growth_pct=None,
                txn_count=0,
                confidence_score=conf,
                fallback_level=2 if d <= 1 else 3,
                is_estimated=True,
            )
            continue

        # Level 4: adjacent floors same month
        adj_rates = []
        for fi2, fdata2 in all_floor_data.items():
            if abs(fi2 - floor_idx) <= 2 and month in fdata2 and not fdata2[month].is_estimated:
                r = fdata2[month].rate_psf
                if r is not None and not pd.isna(r):
                    adj_rates.append(r)
        if adj_rates:
            floor_data[month] = FloorMonthValue(
                rate_psf=_safe_float(np.median(adj_rates)),
                mom_growth_pct=None,
                txn_count=0,
                confidence_score=0.60,
                fallback_level=4,
                is_estimated=True,
            )
            continue

        # Level 5: wider history ±6 months
        wide = [(m, v) for m, v in floor_data.items() if _month_dist(m, month) <= 6]
        if wide:
            closest_m, closest_v = min(wide, key=lambda x: _month_dist(x[0], month))
            floor_data[month] = FloorMonthValue(
                rate_psf=closest_v.rate_psf,
                mom_growth_pct=None,
                txn_count=0,
                confidence_score=0.50,
                fallback_level=5,
                is_estimated=True,
            )
            continue

        # Level 7: project-level average (placeholder None — will be filled later)
        floor_data[month] = FloorMonthValue(
            rate_psf=None,
            mom_growth_pct=None,
            txn_count=0,
            confidence_score=0.30,
            fallback_level=7,
            is_estimated=True,
        )


def build_project_rate_timelapse(
    request: ProjectRateTimelapseRequest,
) -> ProjectRateTimelapseResponse:
    warnings_list: list[str] = []

    excel_path = get_timelapse_data_path()
    df = load_and_preprocess(excel_path, warnings_list)

    if df.empty:
        return _empty_response(warnings_list)

    df = df[df["valid_transaction_flag"]].copy()

    # ── Search / Filter ───────────────────────────────────────────────────────
    search = (request.search or "").strip().lower()
    if search:
        cols = ["project_name", "location_name", "micro_market", "village_name", "city_name"]
        
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

    filter_col("project_name",       request.project_name)
    filter_col("tower_name",         request.tower_name)
    filter_col("property_type",      request.property_type)
    filter_col("unit_configuration", request.unit_configuration)
    filter_col("sale_type",          request.sale_type)

    if request.start_month:
        df = df[df["txn_month"] >= request.start_month]
    if request.end_month:
        df = df[df["txn_month"] <= request.end_month]

    if df.empty:
        return _empty_response(warnings_list, "No transactions matched the given filters.")

    available_projects = sorted(df["project_name"].dropna().unique().tolist()) if "project_name" in df.columns else []
    available_towers   = sorted(df["tower_name"].dropna().unique().tolist())   if "tower_name"   in df.columns else []

    # Need rate + floor
    df = df.dropna(subset=["rate_psf"])
    df = df[df["floor_index"].notna()].copy()
    df["floor_index"] = df["floor_index"].astype(int)

    if df.empty:
        return _empty_response(warnings_list, "No rows with valid rate_psf and floor_index.")

    # ── Aggregation ───────────────────────────────────────────────────────────
    has_tower = "tower_name" in df.columns
    group_cols = ["project_name"] + (["tower_name"] if has_tower else []) + ["floor_index", "txn_month"]

    agg = (
        df.groupby(group_cols, as_index=False, dropna=False)
        .agg(
            avg_rate_psf    =("rate_psf", "mean"),
            median_rate_psf =("rate_psf", "median"),
            txn_count       =("rate_psf", "count"),
            min_rate_psf    =("rate_psf", "min"),
            max_rate_psf    =("rate_psf", "max"),
        )
    )
    # Replace pd.NA with np.nan to avoid float() issues later
    agg = agg.replace({pd.NA: np.nan})

    # ── MoM growth ────────────────────────────────────────────────────────────
    lag_group = ["project_name", "floor_index"] + (["tower_name"] if has_tower else [])
    agg = agg.sort_values(lag_group + ["txn_month"])
    agg["prev_rate"] = agg.groupby(lag_group, dropna=False)["median_rate_psf"].shift(1)
    valid_prev = agg["prev_rate"].notna() & (agg["prev_rate"] > 0)
    agg["mom_growth_pct"] = np.where(
        valid_prev,
        ((agg["median_rate_psf"] - agg["prev_rate"]) / agg["prev_rate"]) * 100,
        np.nan,
    )

    timeline = sorted(agg["txn_month"].unique().tolist())
    all_rates = [r for r in agg["median_rate_psf"].tolist() if not pd.isna(r)]
    global_min = _safe_float(np.min(all_rates), 0.0) if all_rates else 0.0
    global_max = _safe_float(np.max(all_rates), 1.0) if all_rates else 1.0

    # ── Coordinates ───────────────────────────────────────────────────────────
    coord_agg = df.groupby("project_name", as_index=False).agg(
        lat=("project_latitude", "mean"),
        lng=("project_longitude", "mean"),
    )
    coord_map: dict[str, tuple[float, float]] = {}
    for _, row in coord_agg.iterrows():
        if pd.notna(row["lat"]) and pd.notna(row["lng"]):
            coord_map[str(row["project_name"])] = (_safe_float(row["lat"]), _safe_float(row["lng"]))

    # ── Build buildings ───────────────────────────────────────────────────────
    project_key_cols = ["project_name"] + (["tower_name"] if has_tower else [])
    buildings_out: list[BuildingTimelapse] = []

    for keys, grp in agg.groupby(project_key_cols, dropna=False):
        if isinstance(keys, str):
            proj_name  = keys
            tower_name = None
        else:
            proj_name  = keys[0]
            tower_name = keys[1] if len(keys) > 1 else None

        if pd.isna(proj_name):
            continue
        if pd.isna(tower_name):
            tower_name = None

        if proj_name not in coord_map:
            warnings_list.append(f"Skipping '{proj_name}': no valid coordinates.")
            continue

        lat, lng = coord_map[proj_name]

        # floor_index → {month → FloorMonthValue}
        floor_monthly: dict[int, dict[str, FloorMonthValue]] = {}
        for _, row in grp.iterrows():
            fi  = int(row["floor_index"])
            mon = str(row["txn_month"])
            floor_monthly.setdefault(fi, {})[mon] = FloorMonthValue(
                rate_psf        = _safe_float(row["median_rate_psf"]),
                mom_growth_pct  = _safe_float(row.get("mom_growth_pct")),
                txn_count       = int(row["txn_count"]) if pd.notna(row["txn_count"]) else 0,
                confidence_score= 1.0,
                fallback_level  = 1,
                is_estimated    = False,
            )

        # Apply fallback for gaps
        for fi, fdata in floor_monthly.items():
            _apply_fallback(fdata, timeline, floor_monthly, fi)

        # Fill Level 6: project monthly average for still-None rates
        proj_monthly_avg: dict[str, list[float]] = {}
        for fi, fdata in floor_monthly.items():
            for mon, fmv in fdata.items():
                if fmv.rate_psf is not None and not pd.isna(fmv.rate_psf) and not fmv.is_estimated:
                    proj_monthly_avg.setdefault(mon, []).append(fmv.rate_psf)

        # Compute average, ensuring we don't have pd.NA in the mean calculation
        proj_monthly_avg_final: dict[str, float] = {}
        for m, v in proj_monthly_avg.items():
            if v:
                proj_monthly_avg_final[m] = _safe_float(np.mean(v))

        for fi, fdata in floor_monthly.items():
            for mon, fmv in fdata.items():
                if (fmv.rate_psf is None or pd.isna(fmv.rate_psf)) and mon in proj_monthly_avg_final:
                    floor_monthly[fi][mon] = FloorMonthValue(
                        rate_psf=proj_monthly_avg_final[mon],
                        mom_growth_pct=None, txn_count=0,
                        confidence_score=0.40, fallback_level=6, is_estimated=True,
                    )

        floors_list = [
            FloorTimelapse(floor_index=fi, monthly_values=mv)
            for fi, mv in sorted(floor_monthly.items())
        ]
        buildings_out.append(BuildingTimelapse(
            project_id   = None,
            project_name = proj_name,
            tower_name   = tower_name,
            latitude     = lat,
            longitude    = lng,
            floors       = floors_list,
        ))

    if not buildings_out:
        return _empty_response(warnings_list, "No buildings with valid coordinates could be rendered.")

    lats = [b.latitude for b in buildings_out if not pd.isna(b.latitude)]
    lngs = [b.longitude for b in buildings_out if not pd.isna(b.longitude)]
    center_lat = _safe_float(np.mean(lats), _DEFAULT_CENTER[0]) if lats else _DEFAULT_CENTER[0]
    center_lng = _safe_float(np.mean(lngs), _DEFAULT_CENTER[1]) if lngs else _DEFAULT_CENTER[1]

    return ProjectRateTimelapseResponse(
        map_center        = [center_lat, center_lng],
        timeline          = timeline,
        buildings         = buildings_out,
        warnings          = warnings_list,
        available_projects= available_projects[:200],
        available_towers  = available_towers[:200],
        global_min_rate   = global_min,
        global_max_rate   = global_max,
    )
