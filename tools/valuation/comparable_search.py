from __future__ import annotations

"""
Comparable Search Tool (Web Strategy)
Provides comparable_selection_agent() — LLM web-search (GPT-4o-mini, iterative radius expansion)

Confidence scoring (pure Python, no LLM):
  - Location Similarity (40% weight) -> Pure text-based locality/micro-market similarity
  - Property Category (30% weight) -> Broad classes (Residential vs Commercial)
  - Amenities (30% weight) -> Fuzzy match on subject property's amenities list
"""

import json
import math
import re
import logging
import os
import sys
import time

from openai import OpenAI
from dotenv import load_dotenv
from tools.valuation.map_search import search_coordinates

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# ── Confidence Scoring ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _score_location(comp_location: str, subject_location: str) -> int:
    """
    Pure text/name-based location match. NO distance (km) used here at all.
    """
    if not comp_location or not subject_location:
        return 0

    STRIP_WORDS = {
        "the", "a", "an", "of", "in", "at", "near", "and", "by",
        "east", "west", "north", "south", "new", "old",
        "sector", "phase", "block", "tower", "wing", "road", "rd",
        "street", "st", "avenue", "ave", "lane", "marg", "extension",
        "part", "plot", "society", "colony", "nagar", "vihar",
    }

    CITY_WORDS = {
        "pune", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai",
        "delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad",
        "dubai", "abudhabi", "sharjah", "singapore", "london", "toronto",
        "thane", "navi", "pimpri", "chinchwad", "nashik", "nagpur",
    }

    def locality_tokens(s: str) -> list[str]:
        s = s.lower().strip()
        s = re.sub(r'[,\-/\\]+', ' ', s)
        s = re.sub(r'\s+', ' ', s)
        return [
            w for w in s.split()
            if len(w) > 2
            and w not in STRIP_WORDS
            and w not in CITY_WORDS
        ]

    def city_tokens(s: str) -> set[str]:
        s = s.lower()
        return {w for w in re.split(r'[\s,\-/\\]+', s) if w in CITY_WORDS}

    c_loc = locality_tokens(comp_location)
    s_loc = locality_tokens(subject_location)

    if c_loc and s_loc and c_loc[0] == s_loc[0]:
        return 100

    if c_loc and s_loc and (set(c_loc) & set(s_loc)):
        return 100

    c_cities = city_tokens(comp_location)
    s_cities = city_tokens(subject_location)
    if c_cities and s_cities and (c_cities & s_cities):
        return 50

    return 0


def _score_amenities(comp_amenities: list[str], subject_amenities: list[str]) -> int:
    """
    Compare list of amenities of comparable with subject.
    Returns a score 0-100 based on matching ratio.
    """
    if not subject_amenities:
        return 100
    if not comp_amenities:
        return 0

    def clean(a: str) -> str:
        return re.sub(r'[^a-z0-9]', '', a.lower().strip())

    comp_cleaned = {clean(x) for x in comp_amenities if x}
    subj_cleaned = {clean(x) for x in subject_amenities if x}

    if not subj_cleaned:
        return 100

    matched = 0
    for s in subj_cleaned:
        if s in comp_cleaned:
            matched += 1
            continue
        for c in comp_cleaned:
            if s in c or c in s:
                matched += 1
                break

    return round((matched / len(subj_cleaned)) * 100)


def _score_property_category(comp_category: str | None, subject_category: str | None) -> int:
    """
    Compare category family (Residential / Villa / Plot vs Commercial / Retail / Office).
    """
    if not comp_category or not subject_category:
        return 50

    cc = comp_category.strip().lower()
    sc = subject_category.strip().lower()

    if cc == sc:
        return 100

    residential = {"residential", "villa", "plot", "apartment"}
    commercial = {"commercial", "retail", "commercial_office", "office", "shop"}

    if cc in residential and sc in residential:
        return 70
    if cc in commercial and sc in commercial:
        return 70

    return 0


def _confidence_tier(score: int) -> str:
    if score >= 75: return "High"
    if score >= 55: return "Medium"
    if score >= 35: return "Low"
    return "Very Low"


def assign_web_confidence_score(comp: dict, subject: dict) -> dict:
    """
    Confidence scoring SPECIFIC to web-search comparables.
    Only based on:
      - location (40% weight)
      - property category (30% weight)
      - type of amenities (30% weight)
    """
    subject_location = subject.get("location_name") or ""
    subject_type = subject.get("property_type") or "apartment"
    subject_category = subject.get("project_category") or _PROJECT_CATEGORY_DEFAULT.get(subject_type, "Residential")
    subject_amenities = subject.get("amenities") or []

    # 1. Location match score (pure text locality match)
    if subject_location and subject_location.strip():
        loc_score = _score_location(comp.get("location", ""), subject_location)
        loc_fallback = False
    else:
        loc_score = 50
        loc_fallback = True

    # 2. Property category match score
    comp_category = comp.get("project_category") or _PROJECT_CATEGORY_DEFAULT.get(comp.get("property_type", "apartment"), "Residential")
    cat_score = _score_property_category(comp_category, subject_category)
    cat_fallback = not bool(subject_category)

    # 3. Amenities match score
    comp_amenities = comp.get("amenities") or []
    am_score = _score_amenities(comp_amenities, subject_amenities)
    am_fallback = not bool(subject_amenities)

    final_score = round(0.40 * loc_score + 0.30 * cat_score + 0.30 * am_score)

    reasoning = (
        f"Location match score: {loc_score}/100. "
        f"Property category match score: {cat_score}/100 (comparable: '{comp_category}', subject: '{subject_category}'). "
        f"Amenities match score: {am_score}/100."
    )

    comp["confidence_score"]     = final_score
    comp["confidence_tier"]      = _confidence_tier(final_score)
    comp["confidence_reasoning"] = reasoning
    comp["factor_breakdown"]     = {
        "location":           loc_score,
        "property_category":  cat_score,
        "amenities":          am_score,
        "loc_fallback_used":  loc_fallback,
        "cat_fallback_used":  cat_fallback,
        "am_fallback_used":   am_fallback,
    }
    return comp


# ══════════════════════════════════════════════════════════════════════════
# ── Property-type normalisation (public helpers) ──────────────────────────
# ══════════════════════════════════════════════════════════════════════════

_PROP_TYPE_ALIASES: dict[str, set[str]] = {
    "apartment":         {"apartment", "flat", "condo", "condominium", "penthouse"},
    "villa":             {"villa", "bungalow", "row house", "townhouse", "independent house", "independent villa", "house"},
    "plot":              {"plot", "land", "site", "residential plot", "na plot"},
    "retail":            {"shop", "retail", "showroom"},
    "commercial_office": {"office", "workspace", "coworking", "commercial_office"},
    "mixed_use":         {"mixed use", "mixed-use"},
}

_PROJECT_CATEGORY_DEFAULT: dict[str, str] = {
    "apartment":         "Residential",
    "villa":             "Villa",
    "plot":              "Plot",
    "retail":            "Commercial",
    "commercial_office": "Commercial",
    "mixed_use":         "Residential + Commercial",
}


def normalize_property_type(raw: str) -> str | None:
    """Map a free-text property type string to the canonical internal key."""
    if not raw:
        return None
    raw = raw.lower().strip()
    for k, aliases in _PROP_TYPE_ALIASES.items():
        if raw == k or raw in aliases:
            return k
    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in km between two lat/lng points."""
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lng2 - lng1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ══════════════════════════════════════════════════════════════════════════
# ── LLM web-search comparable agent ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """You are a real-estate comparable-selection assistant.
Given a subject property, return a JSON object with a single key "comparables"
whose value is an array of comparable project objects.

Each object MUST have these keys (use null when unknown):
  project_name        – string, exact project name
  location            – string, locality / neighbourhood name
  country             – string
  property_type       – one of: apartment | villa | plot | retail | commercial_office
  project_category    – Residential | Commercial | Villa | Plot
  age_years           – number or null (approximate age in years)
  possession_status   – "Ready to Move" | "Under Construction" | "—"
  source_url          – string or null (a publicly verifiable URL)
  reason              – 1-sentence reason why this is comparable
  amenities           – array of strings (e.g. ["Gym", "Pool", "24/7 Security", "Clubhouse", "Park"])

Return ONLY the JSON. No prose, no markdown fences."""


def _build_user_prompt(subject: dict, radius_km: float, exclude_names: list[str]) -> str:
    prop_type = subject.get("property_type", "apartment")
    excl = ", ".join(f'"{n}"' for n in exclude_names) if exclude_names else "none"
    rate_hint = ""
    if subject.get("rate_basis") == "plot_land":
        rate_hint = "\nIMPORTANT: The subject is a Villa being valued via the Cost Approach — identify PLOT or LAND comparables (not built-up villas) so a plot/land rate can be derived."

    amenities_list = ", ".join(subject.get("amenities", [])) if subject.get("amenities") else "None specified"

    return (
        f"Subject property:\n"
        f"  Name         : {subject.get('project_name', 'Subject Property')}\n"
        f"  Location     : {subject.get('location_name', '')}, {subject.get('country', 'India')}\n"
        f"  Property type: {prop_type}\n"
        f"  Coordinates  : lat={subject.get('lat', 0)}, lng={subject.get('lng', 0)}\n"
        f"  Search radius: {radius_km} km\n"
        f"  Amenities    : [{amenities_list}]\n"
        f"{rate_hint}\n"
        f"Already found (exclude these): [{excl}]\n\n"
        f"Find up to 8 real, verifiable comparable projects within {radius_km} km.\n"
        f"Prefer projects that have similar amenities and are within the same locality.\n"
        f"Prefer projects that have been sold/transacted in the last 2 years.\n"
        f"Do NOT include the subject project itself."
    )


def comparable_selection_agent(
    subject: dict,
    on_progress=None,
    run_logger=None,
    metrics=None,
) -> dict:
    """
    LLM web-search comparable finder (iterative radius expansion).
    """
    subject_lat  = subject.get("lat") or 0
    subject_lng  = subject.get("lng") or 0
    subject_loc  = subject.get("location_name", "")
    subject_type = subject.get("property_type", "apartment")

    RADII      = [2.0, 5.0, 10.0]
    MIN_COMPS  = 3
    MAX_COMPS  = 12

    all_comps: list[dict]  = []
    seen_names: set[str]   = set()
    token_usage            = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    iterations_log: list   = []
    iteration              = 0

    for radius_km in RADII:
        iteration += 1
        logger.info(f"[ComparableAgent] Iteration {iteration} | radius={radius_km} km | found_so_far={len(all_comps)}")

        user_prompt = _build_user_prompt(subject, radius_km, list(seen_names))
        new_comps: list[dict] = []

        try:
            response = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.1,
                max_tokens=2000,
                timeout=45,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
            )
            usage = response.usage
            if usage:
                token_usage["prompt_tokens"]     += usage.prompt_tokens
                token_usage["completion_tokens"] += usage.completion_tokens
                token_usage["total_tokens"]      += usage.total_tokens

            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(raw)
            candidates = parsed.get("comparables") or []

            if run_logger:
                try:
                    run_logger.save_text(
                        "comparable_search",
                        f"iter_{iteration}_radius_{radius_km}km",
                        json.dumps(parsed, indent=2),
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"[ComparableAgent] LLM call failed (iter {iteration}): {e}")
            candidates = []

        for cand in candidates:
            pname = (cand.get("project_name") or "").strip()
            if not pname:
                continue

            pname_key = re.sub(r'[^a-z0-9]', '', pname.lower())
            if pname_key in seen_names:
                continue
            seen_names.add(pname_key)

            loc   = cand.get("location", "") or subject_loc
            cntry = cand.get("country", "") or subject.get("country", "India")
            geo = search_coordinates(
                location_name=loc,
                country=cntry,
                project_name=pname,
                stage="Stage 3 - Comparable Geocoding",
            )
            comp_lat = geo.get("lat")
            comp_lng = geo.get("lng")
            geo_src  = geo.get("source", "unknown") if "lat" in geo else "geocode_failed"

            if comp_lat and comp_lng and subject_lat and subject_lng:
                dist_km = round(_haversine_km(subject_lat, subject_lng, comp_lat, comp_lng), 3)
            else:
                dist_km = None

            raw_type  = cand.get("property_type", subject_type)
            canonical = normalize_property_type(raw_type) or subject_type

            comp = {
                "project_name":             pname,
                "location":                 loc,
                "country":                  cntry,
                "property_type":            canonical,
                "project_category":         cand.get("project_category") or _PROJECT_CATEGORY_DEFAULT.get(canonical, "Residential"),
                "age_years":                cand.get("age_years"),
                "possession_status":        cand.get("possession_status") or "—",
                "source_url":               cand.get("source_url"),
                "reason":                   cand.get("reason", "Identified by LLM web search"),
                "location_certainty":       "Sure" if "lat" in geo else "Uncertain",
                "map_search_lat":           comp_lat,
                "map_search_lng":           comp_lng,
                "geocode_source":           geo_src,
                "distance_from_subject_km": dist_km,
                "total_transaction_count":  None,
                "project_id":               None,
                "data_source":              "Web",
                "amenities":                cand.get("amenities") or [],
                "confidence_score":         None,
                "confidence_tier":          None,
                "confidence_reasoning":     None,
                "factor_breakdown":         None,
            }

            # Apply confidence scoring
            assign_web_confidence_score(comp, subject)

            new_comps.append(comp)

            if len(all_comps) + len(new_comps) >= MAX_COMPS:
                break

        all_comps.extend(new_comps)

        log_entry = {
            "iteration":    iteration,
            "radius_km":    radius_km,
            "new_added":    len(new_comps),
            "comps_so_far": len(all_comps),
        }
        iterations_log.append(log_entry)

        if on_progress:
            try:
                on_progress(
                    iteration=iteration,
                    radius_km=radius_km,
                    comps_so_far=len(all_comps),
                    new_added=len(new_comps),
                )
            except Exception:
                pass

        logger.info(
            f"[ComparableAgent] Iter {iteration} done | "
            f"new={len(new_comps)} | total={len(all_comps)} | "
            f"tokens={token_usage['total_tokens']}"
        )

        if len(all_comps) >= MIN_COMPS:
            break

        if iteration < len(RADII):
            time.sleep(0.5)

    all_comps.sort(key=lambda x: x.get("confidence_score") or 0, reverse=True)

    logger.info(
        f"[ComparableAgent] Complete | "
        f"total_comps={len(all_comps)} | "
        f"iterations={iteration} | "
        f"tokens={token_usage}"
    )

    return {
        "comparables":    all_comps,
        "iterations":     iteration,
        "iterations_log": iterations_log,
        "_token_usage":   token_usage,
    }