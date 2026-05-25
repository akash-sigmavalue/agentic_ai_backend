"""
LLM Factorial Analysis Engine
==============================

Step 5 of the PropVal valuation pipeline.

Takes the factorial_data produced by compute_factorial_table() and passes it
to GPT-4o with rich semantic context (road interpretation, CBD zone labels,
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
        "BCR > 50% — Dense urban core. Very high land cost, minimal open space, strong "
        "commercial footfall. Premium for office and retail; potential congestion discount "
        "for large-format or family residential."
    ),
    "High Density": (
        "BCR 35–50% — Established urban neighbourhood with good infrastructure, moderate "
        "green cover, and active street life. Positive for most property types."
    ),
    "Medium Density": (
        "BCR 20–35% — Suburban / semi-urban area with balanced development, reasonable "
        "amenity access, and some open space. Neutral baseline for residential."
    ),
    "Low Density": (
        "BCR 8–20% — Emerging or low-rise suburban zone. Lower infrastructure intensity; "
        "positive for plotted development and gated communities."
    ),
    "Very Low / Rural": (
        "BCR < 8% — Peri-urban fringe or rural. Significant land-value upside but limited "
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
You are an expert real estate valuation analyst. You follow a strict ReAct reasoning
framework (Thought → Action → Observation → Critique → Revise) at every stage.

═══════════════════════════════════════════════════════════════
FACTOR ADJUSTMENT FRAMEWORK (4 FACTORS) — ReAct REASONING MODE
═══════════════════════════════════════════════════════════════

FACTORS IN SCOPE:
  1. NEIGHBORHOOD AMENITY
  2. ROAD TYPE
  3. BUILTUP DENSITY
  4. CBD SCORE

GUIDING PHILOSOPHY:
  This framework is principles-based and reasoning-driven.
  You must not mechanically look up scores or apply fixed formulas.
  Instead, follow the ReAct loop — Thought, Action, Observation,
  Critique, Revise — at every stage.

  Scores and adjustments should reflect genuine market impact.
  If your reasoning feels forced or a conclusion seems off,
  pause, critique, and revise before moving forward.

═══════════════════════════════════════════════════════════════
REACT LOOP STRUCTURE
═══════════════════════════════════════════════════════════════

At each stage explicitly produce:

  THOUGHT     → What am I trying to determine here? What do I know,
                and what is uncertain or missing?
  ACTION      → What reasoning step, comparison, or inference am I
                performing to move toward a conclusion?
  OBSERVATION → What did I find or conclude from that action?
  CRITIQUE    → Does this conclusion feel right? Am I double-counting,
                over-adjusting, or filling gaps with assumptions?
  REVISE      → Correct if critique flagged an issue. If conclusion
                holds, explicitly confirm and move on.

Apply this loop at THREE stages:
  STAGE 1 — Factor Scoring     (per factor, per project)
  STAGE 2 — Adjustment Calc    (per factor, then aggregate)
  STAGE 3 — Final Reflection   (whole output sense-check)

═══════════════════════════════════════════════════════════════
SECTION A: ADJUSTMENT BOUNDARIES
═══════════════════════════════════════════════════════════════

FACTOR WEIGHT GUIDANCE BY PROPERTY TYPE:

  NEIGHBORHOOD AMENITY  → Low weight for all types.
  ROAD TYPE             → High weight for retail/commercial_office.
                          Moderate for plot. Lower for apartment/villa.
  BUILTUP DENSITY       → Moderate across all types.
                          apartment/commercial_office: higher density = better.
                          villa/plot: lower density = better (invert reasoning).
  CBD SCORE             → Highest weight overall, especially commercial/retail.

AGGREGATE PRINCIPLE:
  - Proportional to magnitude of difference.
  - Conservative when evidence is weak.
  - Avoid compounding same-direction adjustments without clear justification.
  - When in doubt, adjust less.
  - CRITICAL GUARDRAIL 1: If the subject project itself has sufficient direct listings data (Subject listing count >= 10 in the CONFIDENCE EVALUATION INPUTS), the total cumulative net percentage adjustment / Correction Factor MUST NOT exceed ±10% under any circumstance.
  - CRITICAL GUARDRAIL 2: Under any other circumstance, the total cumulative overall net percentage adjustment / Correction Factor MUST NOT exceed ±20% under any circumstance.

═══════════════════════════════════════════════════════════════
SECTION B: FACTOR SCORING — STAGE 1 ReAct LOOP
═══════════════════════════════════════════════════════════════

Score scale: 1 (worst) to 10 (best). Never assign without completing loop.

B1. NEIGHBORHOOD AMENITY
  - Consider breadth, quality, relevance of nearby amenities.
  - Weight quality over raw count.
  - Expect scores to cluster (shared catchment). Diverge only if clearly justified.

B2. ROAD TYPE (OSM Category based)
  - Category A → Local/internal roads  (lowest accessibility)
  - Category B → District roads        (moderate accessibility)
  - Category C → State highway/arterial(good accessibility)
  - Category D → National highway      (highest accessibility)
  - Do NOT mechanically map category to score. Reason what road access means
    for THAT property type. Highway = strong positive for retail; may be neutral
    or slightly negative for villa.

B3. BUILTUP DENSITY
  - This is NOT a simple floor-space ratio. It measures how intensely developed
    the surrounding micro-market is: concentration of buildings, active projects,
    population activity, road fabric, and overall urban texture within the sample radius.
  - PRIMARY signal: congestion_score (0–10). Higher = denser, more active micro-market.
  - SUPPORTING signals: BCR%, used-area ratio, open space ratio, detected buildings.
  - Scoring direction depends on property type:
      apartment / commercial / office: higher intensity → higher score (demand + footfall).
      villa / plot / low-rise: lower intensity → higher score (space, quiet, exclusivity).
  - Congestion score interpretation:
      0–3  → Sparse / emerging micro-market (low urban activity)
      4–5  → Balanced suburban / semi-urban area
      6–7  → Active urban neighbourhood with strong infrastructure
      8–10 → Dense saturated core (high footfall, minimal open space)
  - If congestion_score is missing, use density_class label and BCR as proxies and flag uncertainty.

B4. CBD SCORE
  - Proximity + connectivity to nearest CBD, IT park, employment hub.
  - Closer + better connected = higher score.
  - 1–2 = very distant, poor connectivity. 9–10 = immediate proximity, excellent access.

═══════════════════════════════════════════════════════════════
SECTION C: ADJUSTMENT CALCULATION — STAGE 2 ReAct LOOP
═══════════════════════════════════════════════════════════════

STEP 1 — Market baseline: assess central tendency of comparable scores per factor.
STEP 2 — Identify gaps: compare subject vs baseline. Classify: negligible/moderate/significant.
STEP 3 — Direction & magnitude: assign adjustment per factor using weight guidance.
STEP 4 — Proximity dampening: if all comps within ~1km, reduce NEIGHBORHOOD AMENITY & BUILTUP DENSITY weight.
STEP 5 — Aggregate sense-check: sum adjustments, verify proportionality, scale back if inflated.
STEP 6 — Derive rate: derived_rate = BASE_RATE × (1 + total_adj / 100)

═══════════════════════════════════════════════════════════════
SECTION D: FINAL REFLECTION — STAGE 3 ReAct LOOP
═══════════════════════════════════════════════════════════════

Run one holistic ReAct pass on entire output. Check for:
  - Internal inconsistencies
  - Contradicting adjustments
  - Implausible derived rate
  - Thin reasoning or data gaps

Flag residual uncertainties. Confirm final derived rate.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════

Structure your response EXACTLY as follows:

## STAGE 1: FACTOR SCORING

### [Project Name] — [Factor Name]
THOUGHT: ...
ACTION: ...
OBSERVATION: ...
CRITIQUE: ...
REVISE: ...
FINAL SCORE: X/10

(repeat for every factor × every project)

---

## STAGE 2: ADJUSTMENT CALCULATION

### STEP 1 — Market Baseline
THOUGHT: ...
ACTION: ...
OBSERVATION: ...
CRITIQUE: ...
REVISE: ...

### STEP 2 — Gap Identification
(same loop)

### STEP 3 — Direction & Magnitude
(same loop — state proposed adjustment per factor)

### STEP 4 — Proximity Dampening
(same loop)

### STEP 5 — Aggregate Sense-Check
(same loop — state total adjustment %)

### STEP 6 — Derived Rate
MARKET RATE RANGE: [CURRENCY]X - [CURRENCY]Y/[UNIT]
CALCULATION MIDPOINT: [CURRENCY]X/[UNIT]
TOTAL ADJUSTMENT: X%
DERIVED RATE: [CURRENCY]X/[UNIT]
DERIVED RATE RANGE: [CURRENCY]X - [CURRENCY]Y/[UNIT]

---

## STAGE 3: FINAL REFLECTION

THOUGHT: ...
ACTION: ...
OBSERVATION: ...
CRITIQUE: ...
REVISE: ...

## FINAL ANSWER
DERIVED RATE: [CURRENCY]X/[UNIT]
DERIVED RATE RANGE: [CURRENCY]X - [CURRENCY]Y/[UNIT]
CONFIDENCE: High / Medium / Low
KEY DRIVERS: [top 2 factors that drove the adjustment]
UNCERTAINTIES: [what was inferred vs evidenced — MUST state which confidence rule(s) triggered]

═══════════════════════════════════════════════════════════════
CONFIDENCE SCORING RULES (MANDATORY — evaluate before assigning)
═══════════════════════════════════════════════════════════════

You will be provided with a "CONFIDENCE EVALUATION INPUTS" section in the
user prompt. Use those exact numbers to evaluate the rules below.
You MUST state in the UNCERTAINTIES field which specific rule(s) triggered
your confidence level.

──────────────────────────────────────────────────────────────
HIGH — ALL of the following must be true:
──────────────────────────────────────────────────────────────
  ✓ Subject has 5 or more direct listings (rate_source = listing, not micromarket)
  ✓ 3 or more comparable projects with actual listing data (not micromarket-derived)
  ✓ Total valid listings across subject + all comparables combined ≥ 20
  ✓ All 4 factors (road, CBD, density, amenity) have real evidence for subject
      (no missing congestion_score, no missing CBD data, no unknown road type)
  ✓ Total net adjustment is within ±10%
  ✓ No project (subject or comparable) is micromarket-derived

──────────────────────────────────────────────────────────────
MEDIUM — HIGH criteria not met, but NONE of the LOW triggers apply:
──────────────────────────────────────────────────────────────
  • Subject has 1–2 direct listings
  • OR subject is micromarket-derived but has strong comp listing support (≥ 5 comp listings)
  • OR only 2 comparable projects with listing data
  • OR 1–2 factors are inferred or partially missing
  • OR total net adjustment is between ±10% and ±15%

──────────────────────────────────────────────────────────────
LOW — ANY single one of these triggers LOW immediately:
──────────────────────────────────────────────────────────────
  ✗ Subject has ZERO direct listings (subject rate_source = micromarket)
  ✗ Only 1 comparable project total
  ✗ 3 or more factors are missing or heavily inferred for the subject
  ✗ Total net adjustment exceeds ±15%
  ✗ Fewer than 5 total valid listings across subject + all comparables combined

### CRITICAL JSON REQUIREMENT:
In the JSON block's `factor_breakdown` section, you MUST include the Subject
Property as the first entry in each factor's `projects` list.
For the Subject Property:
- Set `role` to "SUBJECT"
- Set `adjustment` to 0
- Set `value` and `interpretation` to reflect its actual data.
  * For `neighborhood_amenity`: `value` = amenity count (e.g. "12 amenities").
  * For `road_type`: `value` = category (e.g. "Category D").
  * For `builtup_density`: `value` = congestion score or density class (e.g. "Score: 7.5").
  * For `cbd_score`: `value` = distance to nearest CBD hub (e.g. "3.2 km").
"""


# ── Output Schema ─────────────────────────────────────────────────────────────

output_schema = {
    "methodology": "Global Benchmark (Simple Average)",
    "property_type": "<string>",
    "currency": "<string>",
    "area_unit": "<string>",
    "area_type": "<string>",
    "total_listing_count": "<int>",
    "factor_table": [
        {
            "project_name": "<string>",
            "role": "SUBJECT | COMPARABLE",
            "listing_count": "<int>",
            "avg_rate": "<number | null>",
            "distance_km": "<number | null>",
            "rate_derived_from": "<string — mixed | internal_db | listing>",
            "scores": {
                "road_type": "<float 1-10 | null>",
                "cbd_score": "<float 1-10 | null>",
                "builtup_density": "<float 1-10 | null>",
                "neighborhood_amenity": "<float 1-10 | null>"
            }
        }
    ],
    "valuation_details": {
        "base_rate": "<number>",
        "base_rate_range": {"low": "<number>", "high": "<number>"},
        "attribute_weights": {
            "neighborhood_amenity": "<float>",
            "road_type": "<float>",
            "builtup_density": "<float>",
            "cbd_score": "<float>"
        },
        "net_impacts": {
            "neighborhood_amenity": "<float>",
            "road_type": "<float>",
            "builtup_density": "<float>",
            "cbd_score": "<float>"
        },
        "total_net_adjustment": "<float>",
        "derived_rate": "<number>",
        "derived_rate_range": {"low": "<number>", "high": "<number>"},
        "factor_breakdown": {
            "neighborhood_amenity": {
                "projects": [{"name": "<string>", "role": "SUBJECT | COMPARABLE", "distance_km": "<number | null>", "value": "<any>", "interpretation": "<string>", "adjustment": "<float>"}],
                "subject_vs_avg": "<string>",
                "net_impact": "<float>"
            },
            "road_type": {
                "projects": [{"name": "<string>", "role": "SUBJECT | COMPARABLE", "distance_km": "<number | null>", "value": "<any>", "interpretation": "<string>", "adjustment": "<float>"}],
                "subject_vs_avg": "<string>",
                "net_impact": "<float>"
            },
            "builtup_density": {
                "projects": [{"name": "<string>", "role": "SUBJECT | COMPARABLE", "distance_km": "<number | null>", "value": "<any>", "interpretation": "<string>", "adjustment": "<float>"}],
                "subject_vs_avg": "<string>",
                "net_impact": "<float>"
            },
            "cbd_score": {
                "projects": [{"name": "<string>", "role": "SUBJECT | COMPARABLE", "distance_km": "<number | null>", "value": "<any>", "interpretation": "<string>", "adjustment": "<float>"}],
                "subject_vs_avg": "<string>",
                "net_impact": "<float>"
            }
        }
    },
    "subject_final_rate": "<number>",
    "subject_rate_range": {"low": "<number>", "high": "<number>"},
    "confidence": "High | Medium | Low",
    "confidence_triggers": "<string — list each specific rule that determined the confidence level>",
    "reasoning_audit": {
        "stage_1_scoring_thought": "<string>",
        "stage_2_adjustment_thought": "<string>",
        "final_reflection": "<string>",
        "key_drivers": "<string>",
        "uncertainties": "<string>"
    },
    "reconciliation_note": "<string>",
    "project_reports": [
        {
            "project_name": "<string>",
            "report_markdown": "<string>"
        }
    ]
}


# ── User Prompt Builder ───────────────────────────────────────────────────────

def build_user_prompt(
    subject_data: dict,
    comparables_data: list,
    currency: str = "₹",
    area_unit: str = "sqft",
) -> str:
    lines = ["# VALUATION REQUEST\n"]

    # ── Confidence Evaluation Inputs (explicit numbers for rule evaluation) ──
    subject_listing_count  = subject_data.get("listing_count", 0)
    subject_rate_source    = subject_data.get("rate_derived_from", "listing")
    comp_listing_total     = sum(c.get("listing_count", 0) for c in comparables_data)
    n_comps                = len(comparables_data)
    micromarket_comp_count = sum(
        1 for c in comparables_data if c.get("rate_derived_from") == "micromarket"
    )
    total_listings_combined = subject_listing_count + comp_listing_total

    lines.append("## CONFIDENCE EVALUATION INPUTS")
    lines.append(f"- Subject listing count              : {subject_listing_count}")
    lines.append(f"- Subject rate source                : {subject_rate_source}  (listing = direct data; micromarket = inferred from area average)")
    lines.append(f"- Total comparable projects          : {n_comps}")
    lines.append(f"- Total listings across comps        : {comp_listing_total}")
    lines.append(f"- Total listings combined (subj+comp): {total_listings_combined}")
    lines.append(f"- Micromarket-derived comp projects  : {micromarket_comp_count}")
    lines.append("")

    # ── Subject ──────────────────────────────────────────────────────────────
    lines.append("## SUBJECT PROPERTY")
    lines.append(f"- Name                : {subject_data['name']}")
    lines.append(f"- Property Type       : {subject_data['property_type']}")
    lines.append(f"- Rate Derived From: {subject_data.get('rate_derived_from', 'listing')}")
    lines.append(f"- Listing Count       : {subject_listing_count}")
    lines.append(f"- Rate Source         : {subject_rate_source}")
    lines.append(
        f"- Market Rate Range   : {currency}{subject_data['rate_range']['low']:,} - "
        f"{currency}{subject_data['rate_range']['high']:,}/{area_unit} (90% confidence interval)"
    )
    lines.append(f"- Calculation Midpoint: {currency}{subject_data['calculation_rate']:,}/{area_unit}")
    if subject_data.get("map_report_factors"):
        lines.append("- Below-Map Report Factor Evidence:")
        lines.append("```json")
        lines.append(subject_data["map_report_factors"])
        lines.append("```")
    lines.append("")

    # ── Comparables ──────────────────────────────────────────────────────────
    lines.append("## COMPARABLE PROPERTIES")
    for i, comp in enumerate(comparables_data, 1):
        lines.append(f"\n### Comparable {i}: {comp['name']}")
        lines.append(f"- Property Type       : {comp['property_type']}")
        lines.append(f"- Rate Derived From: {comp.get('rate_derived_from', 'listing')}")
        lines.append(f"- Listing Count       : {comp.get('listing_count', 0)}")
        lines.append(f"- Rate Source         : {comp.get('rate_derived_from', 'listing')}")
        lines.append(
            f"- Market Rate Range   : {currency}{comp['rate_range']['low']:,} - "
            f"{currency}{comp['rate_range']['high']:,}/{area_unit} (90% confidence interval)"
        )
        lines.append(f"- Calculation Midpoint: {currency}{comp['calculation_rate']:,}/{area_unit}")
        lines.append(f"- Distance to Subject : {comp.get('distance_to_subject', 'Unknown')}")
        if comp.get("map_report_factors"):
            lines.append("- Below-Map Report Factor Evidence:")
            lines.append("```json")
            lines.append(comp["map_report_factors"])
            lines.append("```")

    lines.append("\n---")
    lines.append(
        "Now run the full ReAct reasoning loop (Stage 1 → Stage 2 → Stage 3) "
        "and derive the final rate for the subject property. Use the market rate "
        "ranges as valuation evidence, and use the calculation midpoint only where "
        "a single number is required by the formula or JSON schema. "
        f"All monetary values are in {currency} and area is in {area_unit}. "
        "The Below-Map Report Factor Evidence JSON is the sole source of truth "
        "for all location factors: road_type, neighborhood_amenity, builtup_density, and cbd_score. "
        "Use the CONFIDENCE EVALUATION INPUTS section to evaluate the confidence rules "
        "defined in the system prompt and assign the correct confidence level. "
        "You MUST populate the `confidence_triggers` field in the JSON output explaining "
        "which specific rules were met or triggered."
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

def llm_factorial_analysis(payload: Dict[str, Any], model: str = "gpt-4o") -> Dict[str, Any]:
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
    print("=" * 100, flush=True)
    print(f"Model: {model}", flush=True)
    print(f"Projects: 1 subject + {len(comparables_projs)} comparables", flush=True)
    print("-" * 100, flush=True)
    print("SYSTEM MESSAGE:", flush=True)
    print(_SYSTEM_PROMPT, flush=True)
    print("-" * 100, flush=True)
    print("USER MESSAGE:", flush=True)
    print(user_prompt, flush=True)
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
                print(f"Failed to parse LLM response: {content}")
                return {"error": "Failed to parse expert report."}

        result["_token_usage"] = {
            "model":              model,
            "prompt_tokens":      usage.prompt_tokens,
            "completion_tokens":  usage.completion_tokens,
            "total_tokens":       usage.total_tokens,
        }

        # Apply guardrail capping for subject project having >=10 listings
        subject_listing_count = expert_subject.get("listing_count", 0)
        result = enforce_adjustment_cap(result, subject_listing_count)

        if "subject_final_rate" in result and "derived_rate" in result.get("valuation_details", {}):
            result["subject_final_rate"] = result["valuation_details"]["derived_rate"]

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
    model: str = "gpt-4o",
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
