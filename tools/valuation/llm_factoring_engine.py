"""
LLM Factorial Analysis Engine
==============================

Step 5 of the PropVal valuation pipeline.

Takes the factorial_data produced by compute_factorial_table() and passes it
to GPT-4o-mini with rich semantic context (road interpretation, CBD zone labels,
density descriptions, amenity category meanings) and the EXACT radius used to
sample each geospatial factor — so the model understands the geographic scope
of every data point.

Returns a structured result containing:
  - Per-comparable factor adjustments (road / CBD / density / amenity)
  - A final reconciled rate for the subject property
  - Per-project narrative reports (≤120 words each)

Designed to work pan-world across all property types:
  apartment | commercial | office | villa | house | land
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logger = logging.getLogger("llm_factoring_engine")
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))


# ── Radius Defaults (metres) ──────────────────────────────────────────────────
DEFAULT_RADII = {
    "road_m":     200,
    "amenity_m":  1000,
    "density_m":  500,
    "cbd_km":     None,
}


# ── Semantic Context Dictionaries ─────────────────────────────────────────────

ROAD_CONTEXT: Dict[str, Dict] = {
    "D": {
        "label": "Motorway / Expressway Frontage",
        "premium_tier": "Very High",
        "description": (
            "Direct access to a motorway, expressway, or trunk road. Maximum commercial "
            "visibility and logistics connectivity. Typically commands a 15–30% rate premium "
            "for commercial/retail/industrial, but may carry a 5–10% noise/air-quality discount "
            "for luxury residential."
        ),
    },
    "C": {
        "label": "Primary Arterial Road",
        "premium_tier": "High",
        "description": (
            "Located on or immediately adjacent to a primary arterial / national highway. "
            "High traffic volume, excellent inter-city connectivity, strong retail and office "
            "demand. Typical rate premium: 8–18% for commercial; 5–12% for residential."
        ),
    },
    "B": {
        "label": "Secondary Road",
        "premium_tier": "Moderate",
        "description": (
            "On or near a secondary road with reasonable traffic. Good neighbourhood "
            "connectivity without motorway noise impact. Generally the residential baseline "
            "in most markets; small premium for retail end-caps."
        ),
    },
    "A": {
        "label": "Tertiary / Residential Lane",
        "premium_tier": "Standard / Below Baseline",
        "description": (
            "Internal residential street, service lane, living street or pedestrian way. "
            "Quiet environment is a positive for premium residential but a significant negative "
            "for commercial, retail, or industrial uses due to low traffic and footfall."
        ),
    },
}

DENSITY_CONTEXT: Dict[str, str] = {
    "Very High Density": (
        "BCR > 40% — Dense urban core. Very high land cost, minimal open space, strong "
        "commercial footfall. Premium for office and retail; potential congestion discount "
        "for large-format or family residential."
    ),
    "High Density": (
        "BCR >25% — Established urban neighbourhood with good infrastructure, moderate "
        "green cover, and active street life. Positive for most property types."
    ),
    "Medium Density": (
        "BCR >12% — Suburban / semi-urban area with balanced development, reasonable "
        "amenity access, and some open space. Neutral baseline for residential."
    ),
    "Low Density": (
        "BCR >5% — Emerging or low-rise suburban zone. Lower infrastructure intensity; "
        "positive for plotted development and gated communities."
    ),
    "Very Low / Rural": (
        "BCR >2% — Peri-urban fringe or rural. Significant land-value upside but limited "
        "current amenity and infrastructure. High discount for ready-to-move residential."
    ),
}

CBD_ZONE_CONTEXT: Dict[str, str] = {
    "prime":      "< 2 km — Within walking / short-ride distance of a major CBD or business hub. Highest commercial premium.",
    "excellent":  "2–5 km — Easy commute to CBD. Strong residential and office demand zone.",
    "good":       "5–10 km — Accessible CBD commute corridor. Good mid-market residential micro-market.",
    "moderate":   "10–20 km — Suburban commute zone. Average for office; suitable for affordable residential.",
    "peripheral": "> 20 km — Peripheral or satellite location. Significant CBD-distance discount for office; may suit plotted/industrial.",
}

AMENITY_CONTEXT: Dict[str, str] = {
    "Healthcare":    "Hospitals, clinics — critical welfare factor for residential; moderate for commercial.",
    "Education":     "Schools, colleges, universities — highest weight for family-residential segments worldwide.",
    "Transport":     "Metro stations, bus stops, rail — major premium driver for mid-market residential and CBD office.",
    "Retail":        "Malls, supermarkets — daily-convenience premium for residential; footfall driver for retail-commercial.",
    "Leisure":       "Parks, gardens, waterfronts — quality-of-life premium for premium and luxury residential.",
    "Restaurant":    "Cafes, restaurants, bars — lifestyle premium; key for luxury residential and co-working office.",
    "Entertainment": "Cinemas, theatres, clubs — secondary lifestyle factor; relevant for urban-core residential.",
    "Security":      "Police stations, fire stations — safety premium; universally positive for all residential segments.",
    "IT_Office":     "IT parks, tech campuses, co-working — employment catchment premium for residential within 5 km.",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cbd_zone(dist_km: Optional[float]) -> str:
    if dist_km is None:
        return "unknown"
    if dist_km < 2:
        return "prime"
    if dist_km < 5:
        return "excellent"
    if dist_km < 10:
        return "good"
    if dist_km < 20:
        return "moderate"
    return "peripheral"


def _format_amenity_distance(distance_m: Any) -> Optional[str]:
    try:
        metres = float(distance_m)
    except (TypeError, ValueError):
        return None

    if metres >= 1000:
        km = metres / 1000
        return f"{km:.1f}km" if km % 1 else f"{int(km)}km"
    return f"{int(round(metres))}m"


def _format_neighborhood_amenities(project: Dict[str, Any]) -> str:
    amenities = project.get("amenities") or {}
    total = amenities.get("total", 0)
    radius_m = amenities.get("sample_radius_m", DEFAULT_RADII["amenity_m"])

    details = amenities.get("details") or {}
    if details:
        preferred_order = [
            "bus_stops", "metro_stations", "railway_stations",
            "schools", "colleges",
            "hospitals", "clinics",
            "gardens",
            "malls", "supermarkets",
            "restaurants_entertainment",
            "police_stations", "fire_stations",
            "it_parks",
        ]
        labels = {
            "bus_stops": "Bus Stops",
            "metro_stations": "Metro Stations",
            "railway_stations": "Railway Stations",
            "schools": "Schools",
            "colleges": "Colleges",
            "hospitals": "Hospitals",
            "clinics": "Clinics",
            "gardens": "Parks",
            "malls": "Shopping Malls",
            "supermarkets": "Supermarkets",
            "restaurants_entertainment": "Restaurants",
            "police_stations": "Police Stations",
            "fire_stations": "Fire Stations",
            "it_parks": "IT Parks",
        }

        ordered_keys = [key for key in preferred_order if key in details]
        ordered_keys.extend(sorted(key for key in details if key not in preferred_order))

        parts = []
        for key in ordered_keys:
            info = details.get(key) or {}
            label = labels.get(key, key.replace("_", " ").title())
            count = info.get("count", 0)
            distance = _format_amenity_distance(info.get("nearest_distance_m"))
            parts.append(f"{label} {count} {distance}" if distance else f"{label} {count}")

        if parts:
            return ", ".join(parts)

    return f"{total} amenities within {radius_m}m"


def _format_map_report_factors(project: Dict[str, Any]) -> str:
    evidence = {
        "road": project.get("road") or {},
        "builtup_density": project.get("builtup_density") or {},
        "cbd": project.get("cbd") or {},
        "amenities": project.get("amenities") or {},
    }
    return json.dumps(evidence, ensure_ascii=False, indent=2, default=str)


def _rate_value(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _project_rate_range(project: Dict[str, Any]) -> Dict[str, int]:
    rates = project.get("rates") or {}
    lower = _rate_value(rates.get("ci_90_lower"))
    upper = _rate_value(rates.get("ci_90_upper"))

    if lower is None or upper is None:
        centre = _rate_value(rates.get("avg_rate")) or _rate_value(rates.get("median_rate")) or 0
        lower = upper = centre

    return {
        "low": int(round(min(lower, upper))),
        "high": int(round(max(lower, upper))),
    }


def _project_calculation_rate(project: Dict[str, Any]) -> int:
    rate_range = _project_rate_range(project)
    return int(round((rate_range["low"] + rate_range["high"]) / 2))


def _enrich_project_row(
    row: Dict[str, Any],
    radii: Dict[str, Any],
) -> Dict[str, Any]:
    project: Dict[str, Any] = {
        "project_name":  row.get("project_name", "Unknown"),
        "is_subject":    row.get("is_subject", False),
        "role":          "SUBJECT" if row.get("is_subject") else "COMPARABLE",
        "listing_count": row.get("listing_count", 0),
        "distance_km":   row.get("dist_km") or 0.0,
        "rate_derived_from": row.get("rate_derived_from", "listing"),
        "rates": {
            "avg_rate":    row.get("avg_rate"),
            "median_rate": row.get("median_rate"),
            "p90_rate":    row.get("p90_rate"),
            "ci_90_lower": row.get("ci_90_lower"),
            "ci_90_upper": row.get("ci_90_upper"),
            "rate_derived_from": row.get("rate_derived_from", "listing"),
        },
    }

    # ── Road Factor ──────────────────────────────────────────────────────────
    road_cat = row.get("road_type")
    road_info = ROAD_CONTEXT.get(road_cat) if road_cat else None
    project["road"] = {
        "category":        road_cat or "Unknown",
        "label":           road_info["label"] if road_info else "Data unavailable",
        "premium_tier":    road_info["premium_tier"] if road_info else "Unknown",
        "description":     road_info["description"] if road_info else "No road data.",
        "sample_radius_m": radii.get("road_m", DEFAULT_RADII["road_m"]),
    }

    # ── CBD Factor ───────────────────────────────────────────────────────────
    cbd_data: List[Dict] = row.get("cbd_data") or []
    cbd_list = []
    for cbd in cbd_data[:4]:
        d_km = cbd.get("distance_km")
        zone = _cbd_zone(d_km)
        cbd_list.append({
            "name":         cbd.get("name", ""),
            "short_name":   cbd.get("short_name", ""),
            "type":         cbd.get("type", "commercial_hub"),
            "distance_km":  d_km,
            "zone":         zone,
            "zone_context": CBD_ZONE_CONTEXT.get(zone, ""),
        })
    nearest_cbd_km = cbd_list[0]["distance_km"] if cbd_list else None
    project["cbd"] = {
        "nearest_km":   nearest_cbd_km,
        "nearest_zone": _cbd_zone(nearest_cbd_km),
        "hubs":         cbd_list,
        "measurement":  "straight-line (haversine) distance to geocoded CBD centroid",
    }

    # ── Built-Up Density Factor ──────────────────────────────────────────────
    bd = row.get("builtup_density") or {}
    congestion    = bd.get("congestion") or {}
    metrics       = bd.get("metrics") or {}
    density_class = metrics.get("density_class") or congestion.get("level", "Unknown")
    bcr_pct       = round((metrics.get("building_coverage_ratio") or 0) * 100, 1)
    open_space_pct = round((metrics.get("true_open_space_ratio") or 0) * 100, 1)
    project["builtup_density"] = {
        "density_class":        density_class,
        "description":          DENSITY_CONTEXT.get(density_class, "No density data."),
        "bcr_pct":              bcr_pct,
        "congestion_score":     congestion.get("score"),
        "congestion_level":     congestion.get("level"),
        "open_space_ratio_pct": open_space_pct,
        "detected_buildings":   metrics.get("detected_buildings"),
        "sample_radius_m":      radii.get("density_m", DEFAULT_RADII["density_m"]),
    }

    # ── Amenity Factor ────────────────────────────────────────────────────────
    amenity_summary = row.get("amenity_summary") or {}
    if isinstance(amenity_summary, str):
        try:
            amenity_summary = json.loads(amenity_summary)
        except Exception:
            amenity_summary = {}
    counts_raw = amenity_summary.get("counts") or {}
    if isinstance(counts_raw, str):
        try:
            counts_raw = json.loads(counts_raw)
        except Exception:
            counts_raw = {}

    _cat_map = {
        "hospitals": "Healthcare", "clinics": "Healthcare",
        "schools": "Education", "colleges": "Education",
        "metro_stations": "Transport", "bus_stops": "Transport", "railway_stations": "Transport",
        "malls": "Retail", "supermarkets": "Retail",
        "gardens": "Leisure",
        "restaurants_entertainment": "Restaurant",
        "police_stations": "Security", "fire_stations": "Security",
        "it_parks": "IT_Office",
    }
    category_totals: Dict[str, int] = {}
    for raw_key, cnt in counts_raw.items():
        typed = _cat_map.get(raw_key, raw_key.replace("_", " ").title())
        category_totals[typed] = category_totals.get(typed, 0) + int(cnt or 0)

    amenities_enriched: Dict[str, Any] = {}
    for cat, cnt in sorted(category_totals.items()):
        amenities_enriched[cat] = {
            "count":   cnt,
            "context": AMENITY_CONTEXT.get(cat, ""),
        }

    amenities_raw = row.get("amenities") or []
    if isinstance(amenities_raw, str):
        try:
            amenities_raw = json.loads(amenities_raw)
        except Exception:
            amenities_raw = []

    amenity_details: Dict[str, Dict[str, Any]] = {}
    nearby_amenities: List[Dict[str, Any]] = []
    if isinstance(amenities_raw, list):
        for amenity in amenities_raw:
            if not isinstance(amenity, dict):
                continue
            raw_key = amenity.get("category") or amenity.get("type") or amenity.get("mapped_type")
            if not raw_key:
                continue
            raw_key = str(raw_key)
            nearby_amenities.append({
                "name":        amenity.get("name"),
                "category":    raw_key,
                "mapped_type": amenity.get("mapped_type"),
                "distance_m":  amenity.get("distance_m"),
            })
            detail = amenity_details.setdefault(
                raw_key,
                {"count": int(counts_raw.get(raw_key, 0) or 0), "nearest_distance_m": None, "nearest_name": None},
            )
            distance_m = amenity.get("distance_m")
            try:
                distance_value = float(distance_m)
            except (TypeError, ValueError):
                distance_value = None

            if distance_value is not None and (
                detail["nearest_distance_m"] is None
                or distance_value < detail["nearest_distance_m"]
            ):
                detail["nearest_distance_m"] = distance_value
                detail["nearest_name"] = amenity.get("name")

    for raw_key, cnt in counts_raw.items():
        detail = amenity_details.setdefault(
            raw_key,
            {"count": 0, "nearest_distance_m": None, "nearest_name": None},
        )
        detail["count"] = max(int(cnt or 0), int(detail.get("count") or 0))

    project["amenities"] = {
        "total":           amenity_summary.get("total", sum(category_totals.values())),
        "by_category":     amenities_enriched,
        "details":         amenity_details,
        "nearby":          nearby_amenities,
        "sample_radius_m": radii.get("amenity_m", DEFAULT_RADII["amenity_m"]),
    }

    return project


# ── Main Payload Builder ──────────────────────────────────────────────────────

def build_factoring_payload(
    factorial_data: Dict[str, Any],
    subject: Dict[str, Any],
    comparables: List[Dict[str, Any]],
    radii: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if radii is None:
        radii = {}
    effective_radii = {**DEFAULT_RADII, **{k: v for k, v in radii.items() if v is not None}}

    table: List[Dict] = factorial_data.get("table") or []
    currency      = factorial_data.get("currency", "INR")
    area_unit     = factorial_data.get("area_unit", "sqft")
    area_type     = factorial_data.get("area_type", "Built-up Area")
    rate_basis    = factorial_data.get("rate_basis", "built_up")
    property_type = (
        subject.get("property_type")
        or (comparables[0].get("property_type") if comparables else None)
        or "residential"
    ).lower()

    enriched_projects = [_enrich_project_row(row, effective_radii) for row in table]

    return {
        "currency":       currency,
        "area_unit":      area_unit,
        "area_type":      area_type,
        "rate_basis":     rate_basis,
        "property_type":  property_type,
        "total_listings": factorial_data.get("total_valid", 0),
        "radii_used": {
            "road_sample_radius_m":    effective_radii.get("road_m"),
            "amenity_sample_radius_m": effective_radii.get("amenity_m"),
            "density_sample_radius_m": effective_radii.get("density_m"),
            "cbd_distance":            "straight-line km to geocoded CBD centroid",
        },
        "projects": enriched_projects,
    }


# ── System Prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are an expert real estate valuation analyst. Your task is to factor each
COMPARABLE project's market rate toward the SUBJECT property using 4 geospatial
factors, then derive the subject's final rate using a confidence-weighted blend.

═══════════════════════════════════════════════════════════════
PHASE 1 — FACTOR EACH COMPARABLE'S RATE
═══════════════════════════════════════════════════════════════

For EACH comparable, compare it against the subject on 4 factors:
  1. ROAD TYPE         (max ±5% impact)
  2. AMENITY           (max ±5% impact)
  3. BUILTUP DENSITY   (max ±5% impact)
  4. CBD               (max ±5% impact)

RULES:
  - Each factor adjustment is capped at ±5%.
  - Total net adjustment across all 4 factors for one comparable: capped at ±20%.
  - Direction: if the subject is BETTER than the comparable on a factor, apply a
    POSITIVE adjustment (the comparable rate is adjusted UP toward subject's level).
    If the subject is WORSE, apply a NEGATIVE adjustment (comparable adjusted DOWN).
  - Factored Rate = Comparable_Avg_Rate × (1 + total_net_adj / 100)
  - Be conservative. If difference is negligible, set adjustment to 0.
  - Reason explicitly for every adjustment. Don't apply mechanical formulas.

FACTOR SCORING GUIDANCE:

  ROAD TYPE (categories A < B < C < D):
    Compare subject vs comparable road category. One grade difference ≈ 2–4%.
    For residential: road matters less. For retail/commercial: road matters more.

  AMENITY:
    Compare quality and breadth of nearby amenities.
    Shared catchment areas (projects within 1 km) often have similar amenities —
    reduce or eliminate adjustment in that case.

  BUILTUP DENSITY:
    Compare congestion_score / density_class.
    For apartment/commercial: higher density = better (positive if subject denser).
    For villa/plot: lower density = better (invert).
    If both projects are in the same density band, adjustment should be small.

  CBD:
    Compare nearest CBD distance. Closer = premium.
    > 5 km difference in CBD proximity = meaningful adjustment (up to ±4%).
    < 2 km difference = minimal adjustment.

═══════════════════════════════════════════════════════════════
PHASE 2 — WEIGHTED BLENDING (w1 × subject_rate + w2 × factored_comp_avg)
═══════════════════════════════════════════════════════════════

After factoring all comparables, compute factored_comp_avg = simple average of
all factored comparable rates.

Then compute the final subject rate as:
  final_rate = w1 × subject_own_rate + w2 × factored_comp_avg
  where w1 + w2 = 1.0

WEIGHT DETERMINATION RULES (data-driven, dynamic calculation):

  You must dynamically compute w1 (subject weight) and w2 (comparable weight, where w2 = 1.0 - w1) using the following scoring logic:

  1. Establish Base Weight (from Subject Listing Count):
     - High Sample (≥ 10 listings):      Base w1 = 0.85
     - Moderate Sample (5–9 listings):    Base w1 = 0.75
     - Low Sample (2–4 listings):         Base w1 = 0.65
     - Minimal/Fallback (0–1 listings or micromarket-derived): Hard w1 = 0.50 (No CI modifiers apply)

  2. Apply Confidence Interval (CI) Modifier (width as % of avg rate):
     - Narrow CI (width < 15% of average): Add +0.05 to +0.10 (rewards low variance/high consensus)
     - Moderate CI (width 15% to 25% of average): No modification (+0.00)
     - Wide CI (width > 25% of average): Subtract -0.05 to -0.10 (penalizes high variance/noise)

  3. Enforce Boundaries:
     - Upper Cap: w1 cannot exceed 0.90 (at least 10% weight always goes to market comparables).
     - Lower Floor: w1 cannot fall below 0.50 for properties with ≥ 2 listings.
     - Sum Constraint: w1 + w2 must equal exactly 1.0 (strictly verify that w1 + w2 = 1.0 before final calculation).

  You MUST show your step-by-step reasoning in the output:
  - Show the base w1 selected.
  - Show the CI width percentage and the resulting modifier.
  - Show the final computed w1 (after applying modifier and boundaries) and w2.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════

First produce a narrative reasoning section, then a JSON block.

Narrative sections:
## PHASE 1: COMPARABLE FACTORING
### [Comparable Name]
- Road Adj: X% — [reason]
- Amenity Adj: X% — [reason]
- Builtup Density Adj: X% — [reason]
- CBD Adj: X% — [reason]
- Total Net Adj: X% (capped at ±20%)
- Factored Rate: [avg_rate] × (1 + X%) = Y
(repeat for each comparable)

## PHASE 2: WEIGHT DETERMINATION
- Subject listing count: N
- CI width: X (= upper - lower)
- CI width as % of avg rate: Y%
- Chosen weights: w1=X, w2=Y
- Reasoning: ...

## PHASE 3: FINAL RATE DERIVATION
- Subject own rate: X
- Factored comp avg: Y
- Final rate = w1×X + w2×Y = Z

## PHASE 4: CONFIDENCE ASSESSMENT
- Confidence Level: [HIGH | MEDIUM | LOW]
- Triggers / Rules Applied: [Explain exactly which rules/triggers from the CONFIDENCE RULES section matched or why they didn't]
- Reasoning: [Brief explanation of the data coverage, sample count, and consistency that supports this confidence level]

═══════════════════════════════════════════════════════════════
CONFIDENCE RULES
═══════════════════════════════════════════════════════════════

HIGH: Subject ≥ 5 listings, ≥ 3 comps with listing data, total listings ≥ 20,
      all 4 factors evidenced, net adj within ±10%, no micromarket source.
MEDIUM: HIGH not met but no LOW trigger applies.
LOW (any one triggers): Subject 0 listings, only 1 comp, 3+ factors missing,
      adj > ±15%, total listings < 5.

═══════════════════════════════════════════════════════════════
CRITICAL JSON REQUIREMENT — comparable_factoring_table
═══════════════════════════════════════════════════════════════

The `comparable_factoring_table` array in the JSON output MUST contain BOTH:
  1. The SUBJECT property as the FIRST entry (role = "SUBJECT")
  2. ALL comparable projects after it (role = "COMPARABLE")

For the subject entry:
  - role            : "SUBJECT"
  - avg_rate        : subject's own avg rate (the base rate, from inputs)
  - road_type       : subject's road category (A/B/C/D)
  - amenity_summary : FULL comma-separated amenity list for subject.
                      Include ALL amenity types present in the geospatial evidence.
                      Format: "Metro:N, School:N, Garden:N, Hospital:N, Mall:N, ..."
                      Do NOT truncate or abbreviate. List every type with count > 0.
  - builtup_density_score : subject's congestion_score (0–10) or null
  - cbd_nearest_km  : subject's nearest CBD distance in km or null
  - cbd_name        : name of the nearest CBD/employment hub (e.g. "Bandra Kurla Complex",
                      "Whitefield IT Park", "Nariman Point"). Use the actual name from the
                      geospatial evidence. If unknown, set to null.
  - factor_road     : null
  - factor_amenity  : null
  - factor_density  : null
  - factor_cbd      : null
  - total_factor    : null
  - factored_rate   : null
  - factor_reasoning: "Subject property — reference baseline, no adjustment applied."

For EACH comparable entry:
  - amenity_summary : FULL comma-separated list of ALL amenity types with count > 0.
                      Do NOT shorten — include Metro, School, Garden, Hospital, Mall,
                      Restaurant, Police Station, IT Park, etc. if present.
  - cbd_nearest_km  : distance to nearest CBD in km
  - cbd_name        : name of the nearest CBD/employment hub for this comparable.

NEVER omit the subject from comparable_factoring_table.
NEVER truncate or abbreviate amenity_summary — list every type.
"""






# ── Output Schema ─────────────────────────────────────────────────────────────

output_schema = {
    "methodology": "Comparable Factoring + Confidence-Weighted Blend",
    "property_type": "<string>",
    "currency": "<string>",
    "area_unit": "<string>",
    "area_type": "<string>",
    "total_listing_count": "<int>",
    "comparable_factoring_table": [
        {
            "project_name": "<string>",
            "role": "SUBJECT | COMPARABLE",
            "road_type": "<string — e.g. A/B/C/D>",
            "amenity_summary": "<string — ALL amenity types listed, e.g. Metro:2, School:3, Garden:1, Hospital:2, Mall:1, Restaurant:4>",
            "builtup_density_score": "<number | null — congestion_score 0-10>",
            "cbd_nearest_km": "<number | null — distance to nearest CBD/employment hub in km>",
            "cbd_name": "<string | null — name of the nearest CBD or major employment hub, e.g. 'Bandra Kurla Complex' or 'Whitefield IT Park'>",
            "avg_rate": "<number | null>",
            "factor_road": "<float — e.g. 0.03 means +3%> | null for subject",
            "factor_amenity": "<float> | null for subject",
            "factor_density": "<float> | null for subject",
            "factor_cbd": "<float> | null for subject",
            "total_factor": "<float — sum of all 4, capped ±0.20> | null for subject",
            "factored_rate": "<number — avg_rate * (1 + total_factor)> | null for subject",
            "factor_reasoning": "<string — MUST start with percentage factors for all 4 attributes like 'Road: +X%, Amenity: +Y%, Density: +Z%, CBD: +W% | ' followed by a brief narrative justification>"
        }
    ],
    "blending": {
        "subject_own_rate": "<number>",
        "subject_ci_lower": "<number>",
        "subject_ci_upper": "<number>",
        "subject_ci_width": "<number>",
        "subject_ci_width_pct": "<float — ci_width / avg_rate * 100>",
        "subject_listing_count": "<int>",
        "factored_comp_avg": "<number — simple avg of all factored comparable rates>",
        "w1": "<float — weight for subject_own_rate, 0 to 1>",
        "w2": "<float — weight for factored_comp_avg, 0 to 1, w1+w2=1>",
        "weight_reasoning": "<string — explain why these weights were chosen>",
        "final_rate_formula": "w1 * subject_own_rate + w2 * factored_comp_avg",
        "final_rate": "<number>"
    },
    "subject_final_rate": "<number>",
    "subject_rate_range": {"low": "<number>", "high": "<number>"},
    "confidence": "High | Medium | Low",
    "confidence_triggers": "<string>",
    "reasoning_audit": {
        "phase_1_factoring_summary": "<string>",
        "phase_2_weight_reasoning": "<string>",
        "phase_3_final_reflection": "<string>",
        "key_drivers": "<string>",
        "uncertainties": "<string>"
    },
    "reconciliation_note": "<string>"
}


# ── User Prompt Builder ───────────────────────────────────────────────────────

def build_user_prompt(
    subject_data: dict,
    comparables_data: list,
    currency: str = "₹",
    area_unit: str = "sqft",
) -> str:
    lines = ["# VALUATION REQUEST\n"]

    # ── Confidence & Weight Inputs ─────────────────────────────────────────
    subject_listing_count  = subject_data.get("listing_count", 0)
    subject_rate_source    = subject_data.get("rate_derived_from", "listing")
    comp_listing_total     = sum(c.get("listing_count", 0) for c in comparables_data)
    n_comps                = len(comparables_data)
    micromarket_comp_count = sum(
        1 for c in comparables_data if c.get("rate_derived_from") == "micromarket"
    )
    total_listings_combined = subject_listing_count + comp_listing_total

    lines.append("## CONFIDENCE & WEIGHT INPUTS")
    lines.append(f"- Subject listing count              : {subject_listing_count}")
    lines.append(f"- Subject rate source                : {subject_rate_source}")
    lines.append(f"- Total comparable projects          : {n_comps}")
    lines.append(f"- Total listings across comps        : {comp_listing_total}")
    lines.append(f"- Total listings combined (subj+comp): {total_listings_combined}")
    lines.append(f"- Micromarket-derived comp projects  : {micromarket_comp_count}")
    lines.append("")

    # ── Subject ───────────────────────────────────────────────────────────
    lines.append("## SUBJECT PROPERTY")
    lines.append(f"- Name                : {subject_data['name']}")
    lines.append(f"- Property Type       : {subject_data['property_type']}")
    lines.append(f"- Rate Derived From   : {subject_rate_source}")
    lines.append(f"- Listing Count       : {subject_listing_count}")
    rate_range = subject_data.get("rate_range", {})
    ci_lower = rate_range.get("low", 0)
    ci_upper = rate_range.get("high", 0)
    avg_rate = subject_data.get("calculation_rate", 0)
    ci_width = ci_upper - ci_lower
    ci_width_pct = round((ci_width / avg_rate * 100), 1) if avg_rate else 0
    lines.append(f"- Avg Rate            : {currency}{avg_rate:,}/{area_unit}")
    lines.append(f"- 90% CI Range        : {currency}{ci_lower:,} — {currency}{ci_upper:,}/{area_unit}")
    lines.append(f"- CI Width            : {ci_width:,} ({ci_width_pct}% of avg) — USED FOR w1/w2 DETERMINATION")
    if subject_data.get("map_report_factors"):
        lines.append("- Geospatial Factor Evidence:")
        lines.append("```json")
        lines.append(subject_data["map_report_factors"])
        lines.append("```")
    lines.append("")

    # ── Comparables ───────────────────────────────────────────────────────
    lines.append("## COMPARABLE PROPERTIES")
    for i, comp in enumerate(comparables_data, 1):
        lines.append(f"\n### Comparable {i}: {comp['name']}")
        lines.append(f"- Rate Derived From   : {comp.get('rate_derived_from', 'listing')}")
        lines.append(f"- Listing Count       : {comp.get('listing_count', 0)}")
        comp_range = comp.get("rate_range", {})
        lines.append(
            f"- 90% CI Range        : {currency}{comp_range.get('low', 0):,} — "
            f"{currency}{comp_range.get('high', 0):,}/{area_unit}"
        )
        lines.append(f"- Avg Rate            : {currency}{comp.get('calculation_rate', 0):,}/{area_unit}")
        lines.append(f"- Distance to Subject : {comp.get('distance_to_subject', 'Unknown')}")
        if comp.get("map_report_factors"):
            lines.append("- Geospatial Factor Evidence:")
            lines.append("```json")
            lines.append(comp["map_report_factors"])
            lines.append("```")

    lines.append("\n---")
    lines.append(
        "TASK: Execute PHASE 1 (factor each comparable's avg_rate toward subject using "
        "road/amenity/builtup_density/cbd — max ±5% each factor, max ±20% total per comparable), "
        "PHASE 2 (determine w1/w2 from subject listing count and CI width as shown in weight table), "
        "PHASE 3 (final_rate = w1 × subject_own_rate + w2 × factored_comp_avg). "
        f"Currency: {currency}. Area unit: {area_unit}. "
        "Use the Geospatial Factor Evidence JSON as sole source of truth for all 4 location factors. "
        "Populate the confidence_triggers field explaining which rules determined confidence level."
    )

    return "\n".join(lines)


def enforce_adjustment_cap(result: Dict[str, Any], subject_listing_count: int) -> Dict[str, Any]:
    """
    If the subject project has >=10 direct listings, the total cumulative net
    adjustment (Correction Factor) must not exceed +/-10%.
    Otherwise, under any other circumstance, the total overall net
    adjustment must not exceed +/-20%.
    This safeguard caps the adjustment and scales the nested factors proportionally.
    """
    val_details = result.get("valuation_details", {})
    total_adj = val_details.get("total_net_adjustment")
    
    if total_adj is not None:
        total_adj = float(total_adj)
        
        # Determine appropriate cap based on listing data sufficiency
        if subject_listing_count >= 10:
            cap_limit = 10.0
            reason_str = f"sufficient direct listings (subject_listing_count={subject_listing_count} >= 10)"
        else:
            cap_limit = 20.0
            reason_str = "global maximum correction limit"
            
        # Check if magnitude of adjustment exceeds the cap limit
        if abs(total_adj) > cap_limit:
            capped_adj = cap_limit if total_adj > 0 else -cap_limit
            logger.info(
                f"[Adjustment Guard] Capping Total Correction Factor from {total_adj}% to {capped_adj}% "
                f"due to {reason_str}."
            )
            
            # Update total net adjustment
            val_details["total_net_adjustment"] = capped_adj
            
            # Proportionally scale individual factor net impacts
            net_impacts = val_details.get("net_impacts", {})
            factor_sum = sum(abs(float(v)) for v in net_impacts.values())
            if factor_sum != 0:
                scale = abs(capped_adj) / factor_sum
                for f in net_impacts:
                    net_impacts[f] = round(float(net_impacts[f]) * scale, 2)
                    
            # Also scale the adjustments inside factor_breakdown
            breakdown = val_details.get("factor_breakdown", {})
            for factor_name, factor_data in breakdown.items():
                if isinstance(factor_data, dict):
                    factor_data["net_impact"] = net_impacts.get(factor_name, 0.0)
                    # Scale project adjustments inside this factor breakdown proportionally
                    projects = factor_data.get("projects", [])
                    for p in projects:
                        if "adjustment" in p and p["adjustment"] is not None:
                            try:
                                p["adjustment"] = round(float(p["adjustment"]) * scale, 2)
                            except Exception:
                                pass
                    
            # Recalculate derived rate
            base_rate = val_details.get("base_rate")
            if base_rate is not None:
                derived_rate = round(float(base_rate) * (1 + capped_adj / 100))
                val_details["derived_rate"] = derived_rate
                result["subject_final_rate"] = derived_rate
                
            # Recalculate derived rate range
            base_range = val_details.get("base_rate_range", {})
            if base_range:
                low = base_range.get("low")
                high = base_range.get("high")
                if low is not None and high is not None:
                    derived_range = {
                        "low": round(float(low) * (1 + capped_adj / 100)),
                        "high": round(float(high) * (1 + capped_adj / 100))
                    }
                    val_details["derived_rate_range"] = derived_range
                    result["subject_rate_range"] = derived_range
                    
            # Stamp the reason on reconciliation note
            note = result.get("reconciliation_note", "")
            result["reconciliation_note"] = (
                f"[Adjustment Guard Applied] Total correction factor capped at {capped_adj}% "
                f"due to {reason_str}. Original adjustment was {total_adj}%. "
                f"{note}"
            )
            
    return result


# ── LLM Call ─────────────────────────────────────────────────────────────────

def llm_factorial_analysis(payload: Dict[str, Any], model: str = "gpt-4o-mini") -> Dict[str, Any]:
    subject_proj      = next(p for p in payload["projects"] if p["is_subject"])
    comparables_projs = [p for p in payload["projects"] if not p["is_subject"]]

    expert_subject = {
        "name":              subject_proj["project_name"],
        "property_type":     payload["property_type"],
        "rate_range":        _project_rate_range(subject_proj),
        "calculation_rate":  _project_calculation_rate(subject_proj),
        "map_report_factors": _format_map_report_factors(subject_proj),
        "listing_count":     subject_proj.get("listing_count", 0),
        "rate_derived_from": subject_proj.get("rate_derived_from", "listing"),
    }

    expert_comparables = []
    for comp in comparables_projs:
        expert_comparables.append({
            "name":               comp["project_name"],
            "property_type":      payload["property_type"],
            "rate_range":         _project_rate_range(comp),
            "calculation_rate":   _project_calculation_rate(comp),
            "distance_to_subject": f"{comp['distance_km']} km",
            "map_report_factors": _format_map_report_factors(comp),
            "listing_count":      comp.get("listing_count", 0),
            "rate_derived_from":  comp.get("rate_derived_from", "listing"),
        })

    user_prompt = build_user_prompt(
        expert_subject,
        expert_comparables,
        currency=payload.get("currency", "₹"),
        area_unit=payload.get("area_unit", "sqft"),
    )

    user_prompt += (
        f"\n\nAfter your reasoning, provide a final JSON block matching the schema "
        f"below for system integration:\n```json\n{json.dumps(output_schema, indent=2)}\n```"
    )

    print("\n" + "=" * 100, flush=True)
    print("[LLM Factoring] PROMPT SENT TO LLM FOR RATE DERIVATION", flush=True)
    print(f"Model: {model} | Projects: 1 subject + {len(comparables_projs)} comparables", flush=True)
    print("=" * 100 + "\n", flush=True)

    try:
        response = _client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
        )
        content = response.choices[0].message.content
        usage   = response.usage

        json_match = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(1))
            result["raw_markdown_report"] = content.split("```json")[0].strip()
        else:
            try:
                result = json.loads(content)
                result["raw_markdown_report"] = "Expert report generated."
            except Exception:
                logger.error(f"[LLM Factoring] Failed to parse response.")
                return {"error": "Failed to parse expert report."}

        result["_token_usage"] = {
            "model":             model,
            "prompt_tokens":     usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens":      usage.total_tokens,
        }

        # ── Normalise final rate from new blending schema ─────────────────
        blending = result.get("blending") or {}
        if blending.get("final_rate"):
            result["subject_final_rate"] = round(float(blending["final_rate"]))
        elif result.get("subject_final_rate") is None:
            # Fallback: compute from blending fields if present
            w1 = float(blending.get("w1") or 0)
            w2 = float(blending.get("w2") or 0)
            s_rate = float(blending.get("subject_own_rate") or 0)
            c_avg  = float(blending.get("factored_comp_avg") or 0)
            if w1 + w2 > 0 and (s_rate or c_avg):
                result["subject_final_rate"] = round(w1 * s_rate + w2 * c_avg)

        # Ensure subject_rate_range is populated
        if not result.get("subject_rate_range"):
            final = result.get("subject_final_rate", 0)
            result["subject_rate_range"] = {
                "low":  round(final * 0.95),
                "high": round(final * 1.05),
            }

        return result

    except json.JSONDecodeError as e:
        logger.error(f"[LLM Factoring] JSON parse failed: {e}")
        raise
    except Exception as e:
        logger.error(f"[LLM Factoring] LLM call failed: {e}")
        raise


# ── Public Entry Point ────────────────────────────────────────────────────────

def run_llm_factoring(
    factorial_data: Dict[str, Any],
    subject: Dict[str, Any],
    comparables: List[Dict[str, Any]],
    radii: Optional[Dict[str, Any]] = None,
    model: str = "gpt-4o-mini",
) -> Dict[str, Any]:
    """
    Full pipeline: build payload → call LLM → return structured factoring result.

    Parameters
    ----------
    factorial_data : dict  — compute_factorial_table() output
    subject        : dict  — original subject project dict
    comparables    : list  — original comparable project dicts
    radii          : dict  — optional override: {road_m, amenity_m, density_m}
    model          : str   — OpenAI model

    Returns
    -------
    dict — factoring result with factor_table, subject_final_rate, project_reports
    """
    payload = build_factoring_payload(factorial_data, subject, comparables, radii)

    n_comps = sum(1 for p in payload["projects"] if not p["is_subject"])
    if n_comps == 0:
        raise ValueError("No comparable projects found in factorial data — cannot run factoring.")

    logger.info(f"[LLM Factoring] Calling {model} with {n_comps} comparables...")
    result = llm_factorial_analysis(payload, model=model)
    result["_payload_summary"] = {
        "property_type": payload["property_type"],
        "rate_basis":    payload.get("rate_basis"),
        "currency":      payload["currency"],
        "area_unit":     payload["area_unit"],
        "radii_used":    payload["radii_used"],
        "n_projects":    len(payload["projects"]),
        "n_comparables": n_comps,
    }
    logger.info(
        f"[LLM Factoring] Done. Subject final rate: {result.get('subject_final_rate')} "
        f"{payload['currency']}/{payload['area_unit']}"
    )
    return result
