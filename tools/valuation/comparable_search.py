from __future__ import annotations

"""
Comparable Selection Agent — LLM-first, globally scalable
"""

import json
import re
import math
import os
import time
import logging
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# ── Logging setup ─────────────────────────────────────────────────────────
# NOTE: logging.basicConfig() is a no-op when uvicorn has already attached
# handlers to the root logger (which happens before any module is imported).
# We configure the named logger directly with its own handlers so that logs
# always appear in the uvicorn terminal regardless of startup order.
_LOG_FMT = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("comparable_agent")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _sh = logging.StreamHandler()
    _sh.setLevel(logging.INFO)
    _sh.setFormatter(_LOG_FMT)
    logger.addHandler(_sh)
    _fh = logging.FileHandler("comparable_agent.log", encoding="utf-8")
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(_LOG_FMT)
    logger.addHandler(_fh)
    logger.propagate = False  # prevent double-printing via uvicorn root logger

_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

# ── Property types ─────────────────────────────────────────────────────────
PROPERTY_TYPE_ALIASES = {
    "apartment":         {"apartment", "flat", "condo", "condominium", "penthouse"},
    "villa":             {"villa", "bungalow", "row house", "townhouse"},
    "plot":              {"plot", "land", "site"},
    "retail":            {"shop", "retail", "showroom"},
    "commercial_office": {"office", "workspace", "coworking"},
    "mixed_use":         {"mixed use", "mixed-use", "residential+commercial", "residential + commercial"},
}

PROPERTY_TYPE_DISPLAY = {
    "apartment":         "apartment (flat / condo / penthouse)",
    "villa":             "villa (bungalow / row house / townhouse) or residential plot",
    "plot":              "plot (land / site) or villa / bungalow / independent house",
    "retail":            "shop (retail space / showroom)",
    "commercial_office": "office space (workspace / coworking)",
    "mixed_use":         "mixed-use (residential + commercial)",
}

PROPERTY_TYPE_SEARCH_TERM = {
    "apartment":         "apartment",
    "villa":             "villa or plot",
    "plot":              "plot or villa",
    "retail":            "shop",
    "commercial_office": "office space",
    "mixed_use":         "mixed-use development",
}

PROPERTY_TYPE_EXCLUSIONS = {
    "apartment": [
        "villa", "bungalow", "plot", "land", "shop",
        "office", "retail", "showroom", "row house", "townhouse"
    ],
    "villa": [
        "apartment", "flat", "condo",
        "shop", "office", "retail", "showroom"
    ],
    "plot": [
        "apartment", "flat",
        "shop", "office", "built-up", "constructed"
    ],
    "retail": [
        "apartment", "flat", "villa", "bungalow", "plot",
        "land", "office", "residential", "condo"
    ],
    "commercial_office": [
        "apartment", "flat", "villa", "bungalow", "plot",
        "land", "shop", "retail", "showroom", "residential"
    ],
    "mixed_use": [
        "purely residential", "purely commercial", "plot", "land"
    ],
}

VALID_PROPERTY_TYPES = set(PROPERTY_TYPE_ALIASES.keys())

# ── Project Category Mapping ───────────────────────────────────────────────
# Maps internal property_type key → default project_category label
PROJECT_CATEGORY_DEFAULT = {
    "apartment":         "Residential",
    "villa":             "Villa",
    "plot":              "Plot",
    "retail":            "Commercial",
    "commercial_office": "Commercial",
    "mixed_use":         "Residential + Commercial",
}

# Normalize whatever the LLM returns for project_category
CATEGORY_NORMALIZE = {
    "residential":              "Residential",
    "commercial":               "Commercial",
    "residential + commercial": "Residential + Commercial",
    "residential+commercial":   "Residential + Commercial",
    "mixed use":                "Residential + Commercial",
    "mixed-use":                "Residential + Commercial",
    "villa":                    "Villa",
    "plot":                     "Plot",
    "independent house":        "Independent House",
}


# ── Drop logger ───────────────────────────────────────────────────────────
def log_drop(stage: str, project_name: str, reason: str, extra: dict = None):
    """
    Centralized drop logger.
    Every time a comparable is dropped, call this.
    Logs to file + console so you can track patterns over time.
    """
    msg = f"[DROP] stage={stage} | project='{project_name}' | reason={reason}"
    if extra:
        msg += f" | detail={json.dumps(extra)}"
    logger.warning(msg)


# ── Helpers ───────────────────────────────────────────────────────────────
def normalize_property_type(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.lower().strip()
    for k, v in PROPERTY_TYPE_ALIASES.items():
        if raw == k or raw in v:
            return k
    return None


def normalize_project_category(raw: str, fallback_ptype: str = "") -> str:
    """
    Normalize the LLM-returned project_category to a clean standard label.
    Falls back to the default for the property type if raw is empty/unknown.
    """
    if raw:
        cleaned = raw.strip().lower()
        if cleaned in CATEGORY_NORMALIZE:
            return CATEGORY_NORMALIZE[cleaned]
        # Return title-cased version as best effort
        return raw.strip().title()
    # Fallback: derive from property_type
    return PROJECT_CATEGORY_DEFAULT.get(fallback_ptype, "Unknown")


def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371
    lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


# ── Prompt ────────────────────────────────────────────────────────────────
def build_prompt(subject: dict) -> tuple[str, str]:
    ptype        = subject["property_type"]
    pname        = subject.get("project_name", "subject property")
    country      = subject.get("country", "")
    location     = subject.get("location_name", "")
    display_name = PROPERTY_TYPE_DISPLAY[ptype]
    search_term  = PROPERTY_TYPE_SEARCH_TERM[ptype]
    exclusions   = PROPERTY_TYPE_EXCLUSIONS.get(ptype, [])
    exclusion_str = ", ".join(exclusions)

    # Subject's own category label for reference
    subject_category = PROJECT_CATEGORY_DEFAULT.get(ptype, "Unknown")

    system_prompt = f"""
You are an expert real estate valuation analyst.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROPERTY TYPE CONSTRAINT — READ THIS FIRST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are looking for: {display_name} ONLY.
Primary search term : "{search_term}"
Valid terms         : {', '.join(PROPERTY_TYPE_ALIASES[ptype])}

DO NOT include anything that is: {exclusion_str}

IMPORTANT RULES:
- A mixed-use building with residential + retail -> EXCLUDE (not purely {search_term})
- A project primarily {search_term} but with small other components -> EXCLUDE if unsure
- When in doubt about a project type -> EXCLUDE IT
- Do NOT label a wrong property as "{search_term}" just to fill the list
- It is BETTER to return 5 correct results than 15 mixed results
- MANDATORY: Use the `web_search_preview` tool to find current real-world projects and coordinates. Do NOT rely solely on internal knowledge.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PROPERTY CATEGORY RULES (fill "project_category" field):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Use exactly ONE of these labels for "project_category":
- "Residential"              → apartment / flat / condo / penthouse
- "Villa"                    → villa / bungalow / row house / townhouse
- "Independent House"        → standalone independent house / duplex
- "Plot"                     → plot / land / site
- "Commercial"               → shop / retail / showroom / office / coworking
- "Residential + Commercial" → building with BOTH residential + commercial floors (mixed-use)

Subject project category : "{subject_category}"
Comparable projects should ideally match or be closely related to this category.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VALUATION LOGIC:
- The BEST comparables are those a buyer would realistically consider as alternatives
- Geographic proximity is the strongest signal:
  -> Properties within 1–3 km are most relevant
  -> Properties beyond 5–8 km ONLY if highly comparable
- Same micro-market is critical

COORDINATE AWARENESS:
- Subject coordinates: Lat {subject['lat']}, Lng {subject['lng']}
- Use these as PRIMARY reference for proximity

OUTPUT FORMAT:
YOU MUST RETURN A STRICT JSON ARRAY OF OBJECTS ONLY.
DO NOT WRITE ANY CONVERSATIONAL TEXT. DO NOT WRITE "Here are the comparables".
START YOUR RESPONSE EXACTLY WITH `[` AND END EXACTLY WITH `]`.

JSON Keys for each object:
- "project_name"      (String)
- "location"          (String)
- "country"           (String, e.g. "{country}")
- "property_type"     (String, MUST be exactly "{ptype}")
- "project_category"  (String, MUST be one of: "Residential", "Villa", "Independent House", "Plot", "Commercial", "Residential + Commercial")
- "age_years"         (String or Number)
- "possession_status" (String: "Ready" or "Under Construction")
- "source_url"        (String, direct listing URL)
- "reason"            (String, short explanation of why it is comparable)
- "location_certainty" (String: "Sure" or "Not Sure")
  Rules — output "Sure" ONLY if ALL of the following are true:
    1. The project name is explicitly stated (not inferred or paraphrased)
    2. No guesswork was needed — project + location appear together on the same page
  Output "Not Sure" if ANY of the following apply:
    - Location is a broad zone, district, or city (e.g. "Rishikesh", "North Delhi")
    - Project name is generic (e.g. "Residential Land", "Plot", "New Project")
    - Location was inferred from nearby landmarks or approximate descriptions
    - Coordinates or map pin are absent or inconsistent with stated location
"""

    user_prompt = f"""
Find 15-25 highly relevant comparable {display_name} projects for:

Project    : {pname}
Location   : {location}, {country}
Coordinates: {subject['lat']}, {subject['lng']}

Remember:
- ONLY {display_name} projects
- Do NOT include: {exclusion_str}
- Geographic proximity is key
- Fill "project_category" for EVERY comparable using the exact labels defined
- Return only genuinely similar {search_term} projects
"""
    return system_prompt, user_prompt


# ── LLM Call ──────────────────────────────────────────────────────────────
def fetch_comparables(subject: dict) -> tuple[list, dict]:
    system_prompt, user_prompt = build_prompt(subject)
    model_name = "gpt-4o-mini"
    try:
        response = _client.responses.create(
            model=model_name,
            instructions=system_prompt,
            input=user_prompt,
            tools=[{"type": "web_search_preview"}],
        )
        raw   = response.output_text.strip()
        comps = parse_json_safely(raw)

        usage = {
            "prompt_tokens":     getattr(response.usage, "input_tokens", 0),
            "completion_tokens": getattr(response.usage, "output_tokens", 0),
            "total_tokens":      getattr(response.usage, "total_tokens", 0),
            "model":             model_name,
            "tool_calls":        3  # web_search_preview
        }
        logger.info(f"[LLM Fetch] Received {len(comps)} raw comparables | tokens={usage['total_tokens']}")

        if not comps and raw:
            logger.warning(f"[LLM Fetch] Zero results! Raw snippet: {raw[:300]}...")

        return comps, usage

    except Exception as e:
        logger.error(f"[LLM Fetch] Failed: {e}")
        return [], {"model": model_name, "tool_calls": 0}


def parse_json_safely(raw: str) -> list:
    """
    Robust JSON array extraction.
    1. Tries to find text inside ```json ... ``` blocks first.
    2. Cleans common LLM errors like trailing commas before parsing.
    3. Falls back to finding the first '[' followed by '{'.
    4. Uses a secondary fallback to extract individual {objects} if array parse fails.
    """
    if not raw:
        return []

    def clean_json_str(s: str) -> str:
        s = s.strip()
        s = re.sub(r',\s*([\]\}])', r'\1', s)
        return s

    # 1. Try to extract from markdown blocks
    matches = re.findall(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    for block in matches:
        try:
            cleaned = clean_json_str(block)
            data = json.loads(cleaned)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except:
            continue

    # 2. Try to find the first '[' followed by '{'
    start_match = re.search(r"\[\s*\{", raw)
    if start_match:
        start = start_match.start()
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "[":
                depth += 1
            elif raw[i] == "]":
                depth -= 1
                if depth == 0:
                    potential_json = raw[start: i + 1]
                    try:
                        data = json.loads(clean_json_str(potential_json))
                        return data if isinstance(data, list) else []
                    except Exception:
                        pass

    # 3. Last Resort Fallback: Extract individual objects
    try:
        objects = []
        potential_objs = re.findall(r"\{[^{}]*\}", raw, re.DOTALL)
        for obj_str in potential_objs:
            try:
                obj_data = json.loads(clean_json_str(obj_str))
                if "project_name" in obj_data:
                    objects.append(obj_data)
            except:
                continue
        if objects:
            logger.info(f"[JSON Parse] Fallback extracted {len(objects)} individual objects")
            return objects
    except Exception as e:
        logger.error(f"[JSON Parse] Fallback failed: {e}")

    return []


# ── Hard Filter ───────────────────────────────────────────────────────────
def hard_filter_by_type(comps: list, required_type: str) -> list:
    """
    Layer 1 defense — programmatic type check.
    Allows certain cross-type matches (e.g., Plot vs Villa).
    """
    valid = []

    ALLOW_CROSS = {
        "plot": {"plot", "villa"},
        "villa": {"villa", "plot"}
    }

    for c in comps:
        raw_type   = c.get("property_type", "")
        normalized = normalize_property_type(raw_type)

        is_allowed = (normalized == required_type) or (
            required_type in ALLOW_CROSS and normalized in ALLOW_CROSS[required_type]
        )

        if is_allowed:
            if normalized:
                c["property_type"] = normalized
            valid.append(c)
        else:
            log_drop(
                stage="hard_filter",
                project_name=c.get("project_name", "unknown"),
                reason="property_type_mismatch",
                extra={
                    "got":      raw_type,
                    "expected": required_type,
                    "location": c.get("location", ""),
                }
            )
    logger.info(f"[Hard Filter] {len(valid)}/{len(comps)} passed type check (allowed cross-types included)")
    return valid


# ── Normalize project_category on all comparables ─────────────────────────
def stamp_project_category(comps: list) -> list:
    """
    Normalize the 'project_category' field returned by the LLM.
    If the LLM didn't return one (or returned garbage), fall back to
    the default derived from property_type.
    Logs a warning whenever the fallback is used.
    """
    for c in comps:
        raw_cat   = c.get("project_category", "")
        ptype_key = c.get("property_type", "")
        normalized = normalize_project_category(raw_cat, fallback_ptype=ptype_key)

        if not raw_cat:
            logger.warning(
                f"[Category] Missing project_category for '{c.get('project_name')}' "
                f"— using fallback '{normalized}'"
            )
        elif normalized != raw_cat.strip():
            logger.info(
                f"[Category] Normalized '{raw_cat}' → '{normalized}' "
                f"for '{c.get('project_name')}'"
            )

        c["project_category"] = normalized

    return comps


# ── Scoring ───────────────────────────────────────────────────────────────
def score_comp(comp: dict, subject: dict) -> float:
    score = 0.0
    d = comp.get("distance_from_subject_km")
    if isinstance(d, (int, float)):
        score += max(0, 10 - d)
    if subject["location_name"].lower() in comp.get("location", "").lower():
        score += 5
    url = comp.get("source_url", "")
    if url and "page=" not in url:
        score += 2
    return score


# ── Main Agent ────────────────────────────────────────────────────────────
def comparable_selection_agent(subject: dict, on_progress=None, run_logger=None, metrics=None) -> dict:

    from tools.valuation.map_search import search_coordinates

    ptype       = subject["property_type"]
    all_comps   = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Derive subject's own category label
    subject_category = PROJECT_CATEGORY_DEFAULT.get(ptype, "Unknown")

    logger.info(
        f"[Agent Start] project='{subject.get('project_name')}' "
        f"type='{ptype}' category='{subject_category}' "
        f"search_term='{PROPERTY_TYPE_SEARCH_TERM[ptype]}' "
        f"location='{subject.get('location_name')}'"
    )

    if run_logger:
        run_logger.save_step("comparable_agent", "input_subject", {
            **subject,
            "subject_category": subject_category,
        })

    # ── Step 1: Multi-pass LLM fetch ──────────────────────────────────────
    for i in range(1):
        logger.info(f"[LLM Fetch] Pass {i + 1}/1")
        comps, usage = fetch_comparables(subject)
        all_comps.extend(comps)
        for k in ["prompt_tokens", "completion_tokens", "total_tokens"]:
            total_usage[k] += usage.get(k, 0)

        if metrics:
            metrics.add_tokens(usage, model_name=usage.get("model", "gpt-4o-mini"))
            if usage.get("tool_calls"):
                metrics.add_tool_call("web_search_preview", cost=0.017)

        if on_progress:
            on_progress(i + 1, 0, len(all_comps), len(comps))

        if run_logger:
            run_logger.save_step("comparable_agent", f"pass_{i+1}_raw", comps)

    logger.info(f"[Multi-pass] Total raw comparables: {len(all_comps)}")

    # ── Step 2: Deduplicate ───────────────────────────────────────────────
    seen, deduped = set(), []
    for c in all_comps:
        name = c.get("project_name", "").lower().strip()
        if not name:
            log_drop(stage="dedup", project_name="(empty)", reason="missing_project_name")
            continue
        if name in seen:
            log_drop(stage="dedup", project_name=c.get("project_name", ""), reason="duplicate")
            continue
        seen.add(name)
        deduped.append(c)

    logger.info(f"[Dedup] {len(deduped)}/{len(all_comps)} unique comparables")

    if run_logger:
        run_logger.save_step("comparable_agent", "deduplicated", deduped)

    # ── Step 3: Hard filter (Layer 1) ─────────────────────────────────────
    type_filtered = hard_filter_by_type(deduped, ptype)

    # ── Step 3b: Normalize project_category ───────────────────────────────
    # LLM fills this during web search; we just clean/normalize here.
    type_filtered = stamp_project_category(type_filtered)

    logger.info(
        f"[Category] Subject category: '{subject_category}' | "
        f"Comparable categories: { {c['project_category'] for c in type_filtered} }"
    )

    if run_logger:
        run_logger.save_step("comparable_agent", "after_category_stamp", type_filtered)

    # ── Step 5: Geocode ───────────────────────────────────────────────────
    logger.info(f"[Geocode] Starting for {len(type_filtered)} comparables")
    for c in type_filtered:
        try:
            res = search_coordinates(
                location_name=c.get("location"),
                country=c.get("country"),
                project_name=c.get("project_name"),
            )
            c["map_search_lat"] = res.get("lat")
            c["map_search_lng"] = res.get("lng")
            c["geocode_source"] = res.get("source")

            # Initialize certainty if missing
            if "location_certainty" not in c:
                c["location_certainty"] = "Not Sure"

            if not c["map_search_lat"] or not c["map_search_lng"]:
                log_drop(
                    stage="geocode",
                    project_name=c.get("project_name", ""),
                    reason="geocode_returned_empty",
                    extra={
                        "location": c.get("location", ""),
                        "country":  c.get("country", ""),
                    }
                )
            

            if c["map_search_lat"] and c["map_search_lng"]:
                logger.info(
                    f"[Geocode] OK '{c['project_name']}' -> "
                    f"({c['map_search_lat']}, {c['map_search_lng']}) | "
                    f"Certainty: {c['location_certainty']}"
                )

        except Exception as e:
            c["map_search_lat"] = None
            c["map_search_lng"] = None
            log_drop(
                stage="geocode",
                project_name=c.get("project_name", ""),
                reason=f"geocode_exception: {str(e)}",
                extra={"location": c.get("location", "")},
            )
        time.sleep(0.2)

    # ── Step 6: Distance calculation ──────────────────────────────────────
    clean = []
    for c in type_filtered:
        lat = c.get("map_search_lat")
        lng = c.get("map_search_lng")

        if not lat or not lng:
            log_drop(
                stage="distance_calc",
                project_name=c.get("project_name", ""),
                reason="no_valid_coordinates",
            )
            continue

        c["distance_from_subject_km"] = calculate_distance(
            subject["lat"], subject["lng"], lat, lng
        )
        clean.append(c)

    logger.info(f"[Distance] {len(clean)} comparables with valid coordinates")

    # ── Step 6b: Remove Subject Project ───────────────────────────────────
    filtered_no_subject = []
    subj_name = (subject.get("project_name") or "").lower().strip()
    subj_lat = subject.get("lat")
    subj_lng = subject.get("lng")

    def clean_name(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r'[^a-z0-9]', '', s)
        for suffix in ["society", "apartment", "apartments", "condo", "condominium", "residency", "villas", "heights", "project"]:
            if s.endswith(suffix):
                s = s[:-len(suffix)]
        return s

    subj_name_clean = clean_name(subj_name)

    for c in clean:
        c_name = (c.get("project_name") or "").lower().strip()
        c_name_clean = clean_name(c_name)
        c_lat = c.get("map_search_lat")
        c_lng = c.get("map_search_lng")

        # 1. Coordinate check (closeness/exact match)
        coords_match = False
        if subj_lat is not None and subj_lng is not None and c_lat is not None and c_lng is not None:
            try:
                lat_diff = abs(float(subj_lat) - float(c_lat))
                lng_diff = abs(float(subj_lng) - float(c_lng))
                # 1e-4 is approx 11 meters, very close (same tower/complex)
                if lat_diff < 1e-4 and lng_diff < 1e-4:
                    coords_match = True
            except (ValueError, TypeError):
                pass

        # 2. Name check
        name_match = False
        if subj_name_clean and c_name_clean:
            if subj_name_clean == c_name_clean or subj_name_clean in c_name_clean or c_name_clean in subj_name_clean:
                name_match = True

        # Drop if it is the subject project itself (name AND lat-long are the same, or exact zero distance match)
        is_subject = False
        if name_match and coords_match:
            is_subject = True
            reason = "name_and_latlong_both_match"
        elif coords_match and c.get("distance_from_subject_km", 999) < 0.02:
            is_subject = True
            reason = "exact_coordinate_and_zero_distance_match"

        if is_subject:
            log_drop(
                stage="subject_filter",
                project_name=c.get("project_name", ""),
                reason=f"is_subject_property ({reason})",
                extra={
                    "subject_name": subj_name,
                    "comp_name": c_name,
                    "distance_km": c.get("distance_from_subject_km")
                }
            )
        else:
            filtered_no_subject.append(c)

    clean = filtered_no_subject
    logger.info(f"[Subject Filter] {len(clean)} comparables remain after removing subject project")

    # ── Step 7: Remove bad URLs ───────────────────────────────────────────
    url_clean = []
    for c in clean:
        url = c.get("source_url", "")
        if "page=" in url:
            log_drop(
                stage="url_filter",
                project_name=c.get("project_name", ""),
                reason="pagination_url_not_direct_listing",
                extra={"url": url},
            )
            continue
        url_clean.append(c)

    logger.info(f"[URL Filter] {len(url_clean)} comparables after URL cleanup")

    # ── Step 8: Rank + 15km filter ────────────────────────────────────────
    ranked = sorted(url_clean, key=lambda x: score_comp(x, subject), reverse=True)

    nearby = []
    for c in ranked:
        d = c.get("distance_from_subject_km", 999)
        if d <= 15:
            nearby.append(c)
        else:
            log_drop(
                stage="distance_filter",
                project_name=c.get("project_name", ""),
                reason="beyond_15km_radius",
                extra={"distance_km": d},
            )

    logger.info(
        f"[Agent Done] Final comparables: {len(nearby)} | "
        f"Total tokens: {total_usage['total_tokens']}"
    )

    if run_logger:
        run_logger.save_step("comparable_agent", "final_comps", nearby)

    return {
        "comparables":        nearby,
        "count":              len(nearby),
        "subject_category":   subject_category,   # ← NEW: subject's category label
        "_token_usage":       total_usage,
    }