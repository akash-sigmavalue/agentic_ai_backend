"""
floor_rate_fallback.py
======================
A standalone, zero-external-dependency module implementing a 7-level hierarchical
fallback system for filling missing floor-level rental rate data in 3D timelapse
building visualizations.

Each (floor, month) cell that is ``None`` is resolved using the first level that
produces a value.  Every cell is wrapped in a rich metadata object containing:
  - rate        : the numeric value (or None for no_data)
  - source      : one of the source enum strings
  - confidence  : float 0.0 – 1.0   
  - note        : human-readable explanation

FRONTEND HINT (do not implement here):
    After this backend work is done, the frontend (Deck.gl PolygonLayer) will map
    the "confidence" field to floor opacity and the "source" field to a hatch pattern:
      confidence 1.0  → opacity 1.0,  no pattern  (actual data)
      confidence 0.85 → opacity 0.85, no pattern  (carry_time)
      confidence 0.70 → opacity 0.70, light hatch (interp_floor)
      confidence 0.60 → opacity 0.60, light hatch (interp_2d)
      confidence ≤0.5 → opacity 0.45, heavy hatch (low confidence)
      confidence 0.0  → opacity 0.0 fill, outline only, "?" tooltip
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any


# ---------------------------------------------------------------------------
# Source enum constants & confidence mapping
# ---------------------------------------------------------------------------
SOURCE_ACTUAL = "actual"
SOURCE_CARRY_TIME = "carry_time"
SOURCE_INTERP_FLOOR = "interp_floor"
SOURCE_INTERP_2D = "interp_2d"
SOURCE_CARRY_TIME_WIDE = "carry_time_wide"
SOURCE_BUILDING_AVG = "building_avg"
SOURCE_GLOBAL_FALLBACK = "global_fallback"
SOURCE_NO_DATA = "no_data"

CONFIDENCE: dict[str, float] = {
    SOURCE_ACTUAL: 1.0,
    SOURCE_CARRY_TIME: 0.85,
    SOURCE_INTERP_FLOOR: 0.70,
    SOURCE_INTERP_2D: 0.60,
    SOURCE_CARRY_TIME_WIDE: 0.50,
    SOURCE_BUILDING_AVG: 0.40,
    SOURCE_GLOBAL_FALLBACK: 0.30,
    SOURCE_NO_DATA: 0.0,
}

# Trusted sources whose values can feed into Level 3+ estimations
TRUSTED_SOURCES: set[str] = {SOURCE_ACTUAL, SOURCE_CARRY_TIME}


# ---------------------------------------------------------------------------
# Helper: cell builder
# ---------------------------------------------------------------------------
def _cell(rate: float | None, source: str, note: str) -> dict[str, Any]:
    """Build a single enriched cell dict.

    Args:
        rate:   The numeric rate value, or None if no data.
        source: One of the SOURCE_* constants.
        note:   Human-readable explanation for the value origin.

    Returns:
        Dict with keys ``rate``, ``source``, ``confidence``, ``note``.
    """
    return {
        "rate": rate,
        "source": source,
        "confidence": CONFIDENCE[source],
        "note": note,
    }


# ---------------------------------------------------------------------------
# Helper: parse_month
# ---------------------------------------------------------------------------
def parse_month(month_str: str) -> date:
    """Convert a ``'YYYY-MM'`` string into a :class:`datetime.date` (day=1).

    Args:
        month_str: String in ``'YYYY-MM'`` format.

    Returns:
        A ``date`` object representing the first day of that month.

    Raises:
        ValueError: If the string is malformed.
    """
    parts = month_str.split("-")
    if len(parts) != 2:
        raise ValueError(f"Expected 'YYYY-MM' format, got '{month_str}'")
    return date(int(parts[0]), int(parts[1]), 1)


# ---------------------------------------------------------------------------
# Helper: month_diff
# ---------------------------------------------------------------------------
def month_diff(a: str, b: str) -> int:
    """Signed month difference between two ``'YYYY-MM'`` strings (a − b).

    A positive result means *a* is after *b*.

    Args:
        a: Target month string.
        b: Source month string.

    Returns:
        Integer number of months separating *a* from *b*.
    """
    da = parse_month(a)
    db = parse_month(b)
    return (da.year - db.year) * 12 + (da.month - db.month)


# ---------------------------------------------------------------------------
# Helper: weighted_average
# ---------------------------------------------------------------------------
def weighted_average(values: list[float], weights: list[float]) -> float:
    """Compute a weighted arithmetic mean.

    Args:
        values:  List of numeric values.
        weights: Corresponding positive weights (same length as *values*).

    Returns:
        The weighted mean.

    Raises:
        ValueError: If inputs are empty or weight sum is zero.
    """
    if not values or not weights or len(values) != len(weights):
        raise ValueError("values and weights must be non-empty and equal length")
    total_weight = sum(weights)
    if total_weight == 0:
        raise ValueError("Sum of weights must be > 0")
    return sum(v * w for v, w in zip(values, weights)) / total_weight


# ---------------------------------------------------------------------------
# Helper: compute_building_avg
# ---------------------------------------------------------------------------
def compute_building_avg(
    trusted_grid: list[list[dict[str, Any] | None]],
    target_month_idx: int,
    num_floors: int,
    allowed_sources: set[str] | None = None,
) -> float | None:
    """Average rate across all floors for a specific month using only trusted data.

    Args:
        trusted_grid:    2D grid ``[date_idx][floor_idx]`` of enriched cells or None.
        target_month_idx: Index into the outer (date) dimension.
        num_floors:       Total number of floors.
        allowed_sources:  Set of source strings whose rates count.  Defaults to
                          :data:`TRUSTED_SOURCES`.

    Returns:
        The average rate, or ``None`` if fewer than 2 floors have qualifying data.
    """
    if allowed_sources is None:
        allowed_sources = TRUSTED_SOURCES

    if target_month_idx < 0 or target_month_idx >= len(trusted_grid):
        return None

    rates: list[float] = []
    for floor_idx in range(num_floors):
        cell = trusted_grid[target_month_idx][floor_idx]
        if cell is not None and cell["source"] in allowed_sources and cell["rate"] is not None:
            rates.append(cell["rate"])

    if len(rates) < 2:
        return None
    return sum(rates) / len(rates)


# ---------------------------------------------------------------------------
# Helper: compute_trend
# ---------------------------------------------------------------------------
def compute_trend(
    trusted_grid: list[list[dict[str, Any] | None]],
    num_floors: int,
    source_month_idx: int,
    target_month_idx: int,
) -> float:
    """Compute monthly trend rate from the building's overall averages.

    Uses only trusted cells.  If either month lacks data, returns 0.

    Args:
        trusted_grid:     2D enriched grid.
        num_floors:        Number of floors.
        source_month_idx:  Index of the month the known floor rate belongs to.
        target_month_idx:  Index of the target (empty) month.

    Returns:
        Per-month rate change (can be negative).  Returns 0 if insufficient data.
    """
    delta_months = target_month_idx - source_month_idx
    if delta_months == 0:
        return 0.0

    src_avg = compute_building_avg(trusted_grid, source_month_idx, num_floors)
    tgt_avg = compute_building_avg(trusted_grid, target_month_idx, num_floors)

    if src_avg is None or tgt_avg is None:
        return 0.0
    return (tgt_avg - src_avg) / delta_months


# ---------------------------------------------------------------------------
# Helper: find_2d_neighbors
# ---------------------------------------------------------------------------
def find_2d_neighbors(
    trusted_grid: list[list[dict[str, Any] | None]],
    target_floor_idx: int,
    target_month_idx: int,
    num_floors: int,
    num_months: int,
    max_floor_delta: int = 4,
    max_month_delta: int = 3,
) -> list[tuple[float, float]]:
    """Find known (rate, weight) pairs within a rectangular search radius.

    Uses Manhattan distance ``|Δfloor| + |Δmonth|`` for weighting.

    Args:
        trusted_grid:     2D enriched grid.
        target_floor_idx: Floor index of the target cell.
        target_month_idx: Date index of the target cell.
        num_floors:       Total floor count.
        num_months:       Total month count.
        max_floor_delta:  Maximum floor distance to search.
        max_month_delta:  Maximum month distance to search.

    Returns:
        List of ``(rate, weight)`` tuples.
    """
    results: list[tuple[float, float]] = []

    for f_delta in range(-max_floor_delta, max_floor_delta + 1):
        fi = target_floor_idx + f_delta
        if fi < 0 or fi >= num_floors:
            continue
        for m_delta in range(-max_month_delta, max_month_delta + 1):
            mi = target_month_idx + m_delta
            if mi < 0 or mi >= num_months:
                continue
            if f_delta == 0 and m_delta == 0:
                continue  # skip self

            cell = trusted_grid[mi][fi]
            if cell is not None and cell["rate"] is not None and cell["source"] in TRUSTED_SOURCES:
                distance = abs(f_delta) + abs(m_delta)
                weight = 1.0 / distance
                results.append((cell["rate"], weight))

    return results


# ---------------------------------------------------------------------------
# Primary function
# ---------------------------------------------------------------------------
def fill_floor_rates(building_data: dict[str, Any]) -> dict[str, Any]:
    """Fill every null rate in a building's floor-rate grid using a 7-level
    hierarchical fallback system.

    The function is **idempotent** — calling it twice on the same data produces
    the same result because it operates on a deep copy of the input and only
    mutates the copy.

    Input shape (per building inside ``building_data``)::

        {
          "name": "...",
          "total_floors": N,
          "floor_rates_by_date": [          # outer index = date index
              [rate_or_none, ...],           # inner index = floor index
              ...
          ]
        }

    The function adds to each building:

    - ``floor_rates_enriched``: same 2D shape but each cell is a dict
      ``{rate, source, confidence, note}``
    - ``fill_summary``: count per source type

    It also re-fills the original ``floor_rates_by_date`` with the resolved
    numeric rates (preserving the flat array shape for backward compatibility).

    Args:
        building_data: A single building dict with ``total_floors``,
                       ``floor_rates_by_date``, and a sibling ``dates`` list
                       (or the dates list passed alongside).

    Returns:
        The mutated building dict with ``floor_rates_enriched`` and
        ``fill_summary`` added.
    """
    # Deep copy to guarantee idempotency if called multiple times
    building = copy.deepcopy(building_data)

    floor_rates_by_date: list[list[float | None]] = building.get("floor_rates_by_date", [])
    num_floors: int = int(building.get("total_floors", 0))
    dates: list[str] = building.get("_dates", [])  # injected by caller

    num_months = len(floor_rates_by_date)
    if num_floors <= 0 or num_months == 0:
        building["floor_rates_enriched"] = []
        building["fill_summary"] = _empty_summary()
        return building

    # ------------------------------------------------------------------
    # Phase 1: Build the trusted grid (only Level 1 = actual values)
    # ------------------------------------------------------------------
    trusted_grid: list[list[dict[str, Any] | None]] = []
    for mi in range(num_months):
        month_row: list[dict[str, Any] | None] = []
        for fi in range(num_floors):
            raw = floor_rates_by_date[mi][fi] if fi < len(floor_rates_by_date[mi]) else None
            if raw is not None:
                month_row.append(_cell(float(raw), SOURCE_ACTUAL, "direct transaction"))
            else:
                month_row.append(None)
        trusted_grid.append(month_row)

    # ------------------------------------------------------------------
    # Phase 2: Level 2 pass — carry_time (same floor ±1–2 months)
    #   Adding carry_time results to trusted_grid so Level 3+ can use them
    # ------------------------------------------------------------------
    for mi in range(num_months):
        for fi in range(num_floors):
            if trusted_grid[mi][fi] is not None:
                continue
            result = _try_carry_time(trusted_grid, fi, mi, num_months, dates, max_delta=2)
            if result is not None:
                trusted_grid[mi][fi] = result

    # ------------------------------------------------------------------
    # Phase 3: Build final enriched grid using levels 3–8 for remaining nulls
    #   (uses trusted_grid which now contains actual + carry_time)
    # ------------------------------------------------------------------
    enriched: list[list[dict[str, Any]]] = []
    summary: dict[str, int] = {
        "total_cells": num_months * num_floors,
        SOURCE_ACTUAL: 0,
        SOURCE_CARRY_TIME: 0,
        SOURCE_INTERP_FLOOR: 0,
        SOURCE_INTERP_2D: 0,
        SOURCE_CARRY_TIME_WIDE: 0,
        SOURCE_BUILDING_AVG: 0,
        SOURCE_GLOBAL_FALLBACK: 0,
        SOURCE_NO_DATA: 0,
    }

    for mi in range(num_months):
        month_enriched: list[dict[str, Any]] = []
        for fi in range(num_floors):
            cell = trusted_grid[mi][fi]
            if cell is not None:
                # Level 1 or 2 already resolved
                month_enriched.append(cell)
                summary[cell["source"]] += 1
                continue

            # Level 3 — neighboring floors, same month
            result = _try_interp_floor(trusted_grid, fi, mi, num_floors)
            if result is not None:
                month_enriched.append(result)
                summary[SOURCE_INTERP_FLOOR] += 1
                continue

            # Level 4 — 2D interpolation (floor + time)
            result = _try_interp_2d(trusted_grid, fi, mi, num_floors, num_months)
            if result is not None:
                month_enriched.append(result)
                summary[SOURCE_INTERP_2D] += 1
                continue

            # Level 5 — same floor, wider time window ±3–6 months + trend
            result = _try_carry_time_wide(trusted_grid, fi, mi, num_floors, num_months, dates)
            if result is not None:
                month_enriched.append(result)
                summary[SOURCE_CARRY_TIME_WIDE] += 1
                continue

            # Level 6 — building average, same month
            result = _try_building_avg(trusted_grid, mi, num_floors)
            if result is not None:
                month_enriched.append(result)
                summary[SOURCE_BUILDING_AVG] += 1
                continue

            # Level 7 — global fallback, nearest month
            result = _try_global_fallback(trusted_grid, mi, num_floors, num_months, dates)
            if result is not None:
                month_enriched.append(result)
                summary[SOURCE_GLOBAL_FALLBACK] += 1
                continue

            # Level 8 — no data
            month_enriched.append(_cell(None, SOURCE_NO_DATA, "no data available"))
            summary[SOURCE_NO_DATA] += 1

        enriched.append(month_enriched)

    # ------------------------------------------------------------------
    # Phase 4: Write back to building
    # ------------------------------------------------------------------
    building["floor_rates_enriched"] = enriched
    building["fill_summary"] = summary

    # Also patch the flat floor_rates_by_date for backward compat
    patched_flat: list[list[float | None]] = []
    for mi in range(num_months):
        row: list[float | None] = []
        for fi in range(num_floors):
            row.append(enriched[mi][fi]["rate"])
        patched_flat.append(row)
    building["floor_rates_by_date"] = patched_flat

    # Remove the injected _dates key
    building.pop("_dates", None)

    return building


# ---------------------------------------------------------------------------
# Level 2: Same floor, adjacent months (±1–2)
# ---------------------------------------------------------------------------
def _try_carry_time(
    grid: list[list[dict[str, Any] | None]],
    floor_idx: int,
    month_idx: int,
    num_months: int,
    dates: list[str],
    max_delta: int = 2,
) -> dict[str, Any] | None:
    """Attempt Level 2: carry from the same floor in adjacent months.

    Prefers ±1 over ±2.  If both directions at equal distance have data,
    averages them.

    Args:
        grid:       Current trusted grid.
        floor_idx:  Floor index.
        month_idx:  Target month index.
        num_months: Total months.
        dates:      List of month label strings.
        max_delta:  Maximum month distance to search.

    Returns:
        Enriched cell dict, or None if no neighbor found.
    """
    for delta in range(1, max_delta + 1):
        prev_idx = month_idx - delta
        next_idx = month_idx + delta

        prev_cell = _get_actual_cell(grid, prev_idx, floor_idx, num_months)
        next_cell = _get_actual_cell(grid, next_idx, floor_idx, num_months)

        if prev_cell is not None and next_cell is not None:
            avg = (prev_cell["rate"] + next_cell["rate"]) / 2.0
            src_label = f"{dates[prev_idx]} & {dates[next_idx]}" if dates else f"±{delta}"
            return _cell(avg, SOURCE_CARRY_TIME, f"averaged from {src_label} (Δ{delta} month)")

        if prev_cell is not None:
            src_label = dates[prev_idx] if dates else f"month-{delta}"
            return _cell(prev_cell["rate"], SOURCE_CARRY_TIME, f"carried from {src_label} (Δ{delta} month)")

        if next_cell is not None:
            src_label = dates[next_idx] if dates else f"month+{delta}"
            return _cell(next_cell["rate"], SOURCE_CARRY_TIME, f"carried from {src_label} (Δ{delta} month)")

    return None


def _get_actual_cell(
    grid: list[list[dict[str, Any] | None]],
    month_idx: int,
    floor_idx: int,
    num_months: int,
) -> dict[str, Any] | None:
    """Retrieve a cell only if it exists and has source == 'actual'."""
    if month_idx < 0 or month_idx >= num_months:
        return None
    cell = grid[month_idx][floor_idx]
    if cell is not None and cell["source"] == SOURCE_ACTUAL and cell["rate"] is not None:
        return cell
    return None


# ---------------------------------------------------------------------------
# Level 3: Neighboring floors, same month (±1–3)
# ---------------------------------------------------------------------------
def _try_interp_floor(
    grid: list[list[dict[str, Any] | None]],
    floor_idx: int,
    month_idx: int,
    num_floors: int,
) -> dict[str, Any] | None:
    """Attempt Level 3: weighted average from neighboring floors in the same month.

    Searches outward: ±1 first, then ±2, then ±3.
    Stops when at least 2 neighbors found OR when any neighbor found beyond ±1.

    Args:
        grid:       Trusted grid.
        floor_idx:  Target floor index.
        month_idx:  Target month index.
        num_floors: Total floor count.

    Returns:
        Enriched cell dict, or None.
    """
    found_rates: list[float] = []
    found_weights: list[float] = []
    found_floors: list[int] = []

    for delta in range(1, 4):  # ±1, ±2, ±3
        for direction in (-1, 1):
            neighbor_fi = floor_idx + delta * direction
            if neighbor_fi < 0 or neighbor_fi >= num_floors:
                continue
            cell = grid[month_idx][neighbor_fi]
            if cell is not None and cell["rate"] is not None and cell["source"] in TRUSTED_SOURCES:
                weight = 1.0 / delta
                found_rates.append(cell["rate"])
                found_weights.append(weight)
                found_floors.append(neighbor_fi + 1)  # 1-based for display

        # Stop when at least 2 neighbors found OR when any neighbor found beyond ±1
        if len(found_rates) >= 2:
            break
        if delta > 1 and len(found_rates) >= 1:
            break

    if not found_rates:
        return None

    avg = weighted_average(found_rates, found_weights)
    weights_display = [round(w, 2) for w in found_weights]
    return _cell(
        round(avg, 2),
        SOURCE_INTERP_FLOOR,
        f"interpolated from floors {found_floors} (weights: {weights_display})",
    )


# ---------------------------------------------------------------------------
# Level 4: 2D interpolation (floor + time)
# ---------------------------------------------------------------------------
def _try_interp_2d(
    grid: list[list[dict[str, Any] | None]],
    floor_idx: int,
    month_idx: int,
    num_floors: int,
    num_months: int,
) -> dict[str, Any] | None:
    """Attempt Level 4: 2D weighted interpolation using Manhattan distance.

    Searches within ±4 floors and ±3 months.  Requires at least 2 known points.

    Args:
        grid:       Trusted grid.
        floor_idx:  Target floor index.
        month_idx:  Target month index.
        num_floors: Total floor count.
        num_months: Total month count.

    Returns:
        Enriched cell dict, or None.
    """
    neighbors = find_2d_neighbors(
        grid, floor_idx, month_idx, num_floors, num_months,
        max_floor_delta=4, max_month_delta=3,
    )
    if len(neighbors) < 2:
        return None

    rates = [r for r, _ in neighbors]
    weights = [w for _, w in neighbors]
    avg = weighted_average(rates, weights)
    return _cell(
        round(avg, 2),
        SOURCE_INTERP_2D,
        f"2D interpolated from {len(neighbors)} points (radius: ±4f ±3m)",
    )


# ---------------------------------------------------------------------------
# Level 5: Same floor, wider time window ±3–6 months + trend
# ---------------------------------------------------------------------------
def _try_carry_time_wide(
    grid: list[list[dict[str, Any] | None]],
    floor_idx: int,
    month_idx: int,
    num_floors: int,
    num_months: int,
    dates: list[str],
) -> dict[str, Any] | None:
    """Attempt Level 5: carry from the same floor ±3–6 months, adjusted by trend.

    Args:
        grid:       Trusted grid.
        floor_idx:  Target floor index.
        month_idx:  Target month index.
        num_floors: Total floor count.
        num_months: Total month count.
        dates:      List of month label strings.

    Returns:
        Enriched cell dict, or None.
    """
    # Search ±3 to ±6 months on the same floor
    best_cell: dict[str, Any] | None = None
    best_delta: int = 0
    best_source_idx: int = -1

    for delta in range(3, 7):  # 3, 4, 5, 6
        for direction in (-1, 1):
            src_idx = month_idx + delta * direction
            if src_idx < 0 or src_idx >= num_months:
                continue
            cell = grid[src_idx][floor_idx]
            if cell is not None and cell["rate"] is not None and cell["source"] in TRUSTED_SOURCES:
                if best_cell is None or delta < best_delta:
                    best_cell = cell
                    best_delta = delta
                    best_source_idx = src_idx

        if best_cell is not None:
            break  # use closest found

    if best_cell is None:
        return None

    # Apply trend correction
    trend = compute_trend(grid, num_floors, best_source_idx, month_idx)
    delta_months = month_idx - best_source_idx
    estimated = best_cell["rate"] + (trend * delta_months)

    src_label = dates[best_source_idx] if dates and best_source_idx < len(dates) else f"idx-{best_source_idx}"
    return _cell(
        round(estimated, 2),
        SOURCE_CARRY_TIME_WIDE,
        f"from {src_label} ±trend correction ({abs(delta_months)} months)",
    )


# ---------------------------------------------------------------------------
# Level 6: Building average, same month
# ---------------------------------------------------------------------------
def _try_building_avg(
    grid: list[list[dict[str, Any] | None]],
    month_idx: int,
    num_floors: int,
) -> dict[str, Any] | None:
    """Attempt Level 6: average of all floors with trusted data this month.

    Requires at least 2 floors with trusted data.

    Args:
        grid:       Trusted grid.
        month_idx:  Target month index.
        num_floors: Total floor count.

    Returns:
        Enriched cell dict, or None.
    """
    avg = compute_building_avg(grid, month_idx, num_floors, TRUSTED_SOURCES)
    if avg is None:
        return None

    # Count how many floors contributed
    count = sum(
        1 for fi in range(num_floors)
        if grid[month_idx][fi] is not None
        and grid[month_idx][fi]["source"] in TRUSTED_SOURCES
        and grid[month_idx][fi]["rate"] is not None
    )
    return _cell(
        round(avg, 2),
        SOURCE_BUILDING_AVG,
        f"building average ({count} floors with data this month)",
    )


# ---------------------------------------------------------------------------
# Level 7: Global fallback — nearest month with building data
# ---------------------------------------------------------------------------
def _try_global_fallback(
    grid: list[list[dict[str, Any] | None]],
    month_idx: int,
    num_floors: int,
    num_months: int,
    dates: list[str],
) -> dict[str, Any] | None:
    """Attempt Level 7: use the nearest month's building average.

    Searches outward from target month until a month with ≥2 trusted-data
    floors is found.

    Args:
        grid:       Trusted grid.
        month_idx:  Target month index.
        num_floors: Total floor count.
        num_months: Total month count.
        dates:      List of month label strings.

    Returns:
        Enriched cell dict, or None.
    """
    for delta in range(1, num_months):
        for direction in (-1, 1):
            src_idx = month_idx + delta * direction
            if src_idx < 0 or src_idx >= num_months:
                continue
            avg = compute_building_avg(grid, src_idx, num_floors, TRUSTED_SOURCES)
            if avg is not None:
                src_label = dates[src_idx] if dates and src_idx < len(dates) else f"idx-{src_idx}"
                return _cell(
                    round(avg, 2),
                    SOURCE_GLOBAL_FALLBACK,
                    f"global fallback from {src_label} (Δ{delta} months)",
                )
    return None


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def _empty_summary() -> dict[str, int]:
    """Return a zeroed fill_summary dict."""
    return {
        "total_cells": 0,
        SOURCE_ACTUAL: 0,
        SOURCE_CARRY_TIME: 0,
        SOURCE_INTERP_FLOOR: 0,
        SOURCE_INTERP_2D: 0,
        SOURCE_CARRY_TIME_WIDE: 0,
        SOURCE_BUILDING_AVG: 0,
        SOURCE_GLOBAL_FALLBACK: 0,
        SOURCE_NO_DATA: 0,
    }
