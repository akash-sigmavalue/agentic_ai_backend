"""
timelapse_preprocessing.py
Shared loader + field-derivation for Project and Location timelapse services.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from core.config import settings


# ── Path helper ──────────────────────────────────────────────────────────────

def get_timelapse_data_path() -> Path:
    """Return the Excel path for timelapse data (reuses transactions_db1.xlsx)."""
    if settings.SPATIAL_ANALYSIS_EXCEL_PATH:
        return Path(settings.SPATIAL_ANALYSIS_EXCEL_PATH)
    raise ValueError(
        "SPATIAL_ANALYSIS_EXCEL_PATH is not configured in .env. "
        "Point it to transactions_db1.xlsx."
    )


# ── Floor number parser ───────────────────────────────────────────────────────

_NAMED_FLOORS = {
    "ground": 0, "g": 0, "gf": 0, "gr": 0,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12,
}
_SKIP_FLOORS = {"parking", "podium", "stilt", "open", "terrace", "utility"}


def parse_floor_number(raw_floor) -> Optional[int]:
    """Convert floor_number (any format) to integer floor_index. Returns None to skip."""
    if pd.isna(raw_floor):
        return None
    s = str(raw_floor).strip().lower()

    # Skip non-unit floors
    if any(x in s for x in _SKIP_FLOORS):
        return None

    # Named exact match
    if s in _NAMED_FLOORS:
        return _NAMED_FLOORS[s]

    # Basement  B / B1 / Basement / Basement-1
    if "basement" in s or re.match(r"^b\d*$", s):
        nums = re.findall(r"\d+", s)
        n = int(nums[0]) if nums else 1
        return -n

    # Ordinal / plain integer: 1 / 1st / 2nd / Floor 8 / 8th floor
    nums = re.findall(r"\d+", s)
    if nums:
        return int(nums[0])

    return None


# ── Main loader ───────────────────────────────────────────────────────────────

def load_and_preprocess(
    excel_path: str | os.PathLike[str],
    warnings_list: list[str],
) -> pd.DataFrame:
    """
    Load Excel, normalise column names, derive:
        txn_month, area_sq_ft_final, rate_psf, floor_index, valid_transaction_flag.
    Returns an empty DataFrame on error.
    """
    if not os.path.exists(excel_path):
        warnings_list.append(f"Excel file not found: {excel_path}")
        return pd.DataFrame()

    try:
        df = pd.read_excel(excel_path)
    except Exception as exc:
        warnings_list.append(f"Failed to read Excel: {exc}")
        return pd.DataFrame()

    # Normalise column names to snake_case lowercase
    df.columns = [
        re.sub(r"\s+", "_", str(c).strip().lower()) for c in df.columns
    ]

    # ── transaction_date → txn_month ─────────────────────────────────────────
    if "transaction_date" not in df.columns:
        warnings_list.append("Required column missing: transaction_date")
        return pd.DataFrame()

    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    df["txn_month"] = df["transaction_date"].dt.to_period("M").astype(str)

    # ── area_sq_ft_final ─────────────────────────────────────────────────────
    gross = pd.to_numeric(df.get("gross_carpet_area_sq_ft", pd.Series(dtype=float)), errors="coerce")
    net   = pd.to_numeric(df.get("net_carpet_area_sq_m",   pd.Series(dtype=float)), errors="coerce")

    area = pd.Series(np.nan, index=df.index, dtype=float)
    area = area.where(~(gross > 0), gross)                  # prefer gross if > 0
    area = area.where(area.notna() | ~(net > 0), net * 10.7639)  # fallback net→sqft
    df["area_sq_ft_final"] = area

    # ── rate_psf ─────────────────────────────────────────────────────────────
    psf   = pd.to_numeric(df.get("price_per_sq_ft_gross_carpet", pd.Series(dtype=float)), errors="coerce")
    price = pd.to_numeric(df.get("agreement_price",              pd.Series(dtype=float)), errors="coerce")

    rate = pd.Series(np.nan, index=df.index, dtype=float)
    rate = rate.where(~(psf > 0), psf)
    calc_mask = rate.isna() & (price > 0) & (df["area_sq_ft_final"] > 0)
    rate = rate.where(~calc_mask, price / df["area_sq_ft_final"])
    df["rate_psf"] = rate

    # ── floor_index ───────────────────────────────────────────────────────────
    if "floor_number" in df.columns:
        df["floor_index"] = df["floor_number"].map(parse_floor_number)
    else:
        df["floor_index"] = None

    # ── valid_transaction_flag ────────────────────────────────────────────────
    dup_flag = pd.Series(False, index=df.index)
    if "is_duplicate" in df.columns:
        dup_str = df["is_duplicate"].astype(str).str.strip().str.lower()
        dup_flag = dup_str.isin(["true", "1", "yes"])

    df["valid_transaction_flag"] = (
        price.gt(0)
        & df["area_sq_ft_final"].gt(0)
        & df["transaction_date"].notna()
        & ~dup_flag
    )

    # ── coerce coordinate columns ─────────────────────────────────────────────
    for col in ("project_latitude", "project_longitude",
                "location_latitude", "location_longitude"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ── final cleanup ─────────────────────────────────────────────────────────
    # Global replacement of pd.NA with np.nan to avoid float() conversion errors
    df = df.replace({pd.NA: np.nan})

    return df
