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
    logger.propagate = False

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
PROJECT_CATEGORY_DEFAULT = {
    "apartment":         "Residential",
    "villa":             "Villa",
    "plot":              "Plot",
    "retail":            "Commercial",
    "commercial_office": "Commercial",
    "mixed_use":         "Residential + Commercial",
}

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
    if raw:
        cleaned = raw.strip().lower()
        if cleaned in CATEGORY_NORMALIZE:
            return CATEGORY_NORMALIZE[cleaned]
        return raw.strip().title()
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
- MANDATORY: Use the `web_search_preview` tool to find current real-world projects, coordinates, amenities, and details. Do NOT rely solely on internal knowledge.
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
- "amenities"          (String, brief list of major amenities offered by the comparable project, e.g., "Clubhouse, Pool, Gym, 24/7 Security" or "None / Not found")
- "location_match"     (String, comparison of comparable micro-market/street relative to subject: "Same street" / "Same suburb" / "Same district" / "Same city" / "Different city" / "Not found")
- "amenities_match"    (String, comparison of comparable's amenities to typical expected standard for this type/location: "Exact" / "Close" / "Partial" / "None" / "Not found")
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
- Fill "location_certainty" for EVERY comparable using the exact rules defined — this is MANDATORY
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
            "tool_calls":        3
        }
        logger.info(f"[LLM Fetch] Received {len(comps)} raw comparables | tokens={usage['total_tokens']}")

        if not comps and raw:
            logger.warning(f"[LLM Fetch] Zero results! Raw snippet: {raw[:300]}...")

        return comps, usage

    except Exception as e:
        logger.error(f"[LLM Fetch] Failed: {e}")
        return [], {"model": model_name, "tool_calls": 0}


def parse_json_safely(raw: str) -> list:
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


# ── Location certainty resolver ───────────────────────────────────────────
def is_specific_location(location: str) -> bool:
    if not location:
        return False

    loc = location.lower().strip()
    broad_terms = {
        "india", "pune", "mumbai", "delhi", "bangalore", "bengaluru",
        "hyderabad", "chennai", "kolkata", "ahmedabad", "gurgaon",
        "gurugram", "noida", "north delhi", "south delhi", "rishikesh"
    }
    if loc in broad_terms:
        return False

    specific_markers = [
        ",", "road", "rd", "sector", "phase", "street", "nagar",
        "wadi", "pur", "gaon", "village", "layout", "colony"
    ]
    return any(marker in loc for marker in specific_markers) or len(loc.split()) >= 2


def resolve_location_certainty(comp: dict) -> dict:
    llm_certainty = comp.get("location_certainty")
    comp["llm_location_certainty"] = llm_certainty

    has_project      = bool((comp.get("project_name") or "").strip())
    has_coords       = comp.get("map_search_lat") is not None and comp.get("map_search_lng") is not None
    has_distance     = isinstance(comp.get("distance_from_subject_km"), (int, float))
    specific_location = is_specific_location(comp.get("location", ""))

    if llm_certainty == "Sure":
        comp["location_certainty"] = "Sure"
        comp["location_certainty_reason"] = "LLM found project and location together on source page."
    elif has_project and has_coords and has_distance and specific_location:
        comp["location_certainty"] = "Sure"
        comp["location_certainty_reason"] = (
            "Upgraded after backend geocoding: project has valid coordinates, "
            "a calculated subject distance, and a specific location."
        )
    else:
        comp["location_certainty"] = "Not Sure"
        missing = []
        if not has_project:
            missing.append("project name")
        if not has_coords:
            missing.append("coordinates")
        if not has_distance:
            missing.append("distance")
        if not specific_location:
            missing.append("specific location")
        comp["location_certainty_reason"] = (
            "Could not verify " + ", ".join(missing) if missing else "LLM marked location as uncertain."
        )

    return comp


# ══════════════════════════════════════════════════════════════════════════
# ── Confidence Scoring Agent ──────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

_CONFIDENCE_MODEL    = "gpt-4o-mini"
_CONFIDENCE_TOKENS   = 4096
_CONFIDENCE_BATCH    = 10      # Increased from 5 — no search tools needed, highly efficient batching
_CONFIDENCE_RETRIES  = 2
_CONFIDENCE_DELAY    = 3


def _build_confidence_prompt(subject: dict, batch: list[dict]) -> tuple[str, str]:
    """Build system + user prompt for a confidence-scoring batch."""

    system_prompt = """
You are a senior real estate valuation analyst with deep expertise in comparable property analysis.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR TASK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Given a SUBJECT PROPERTY and a list of COMPARABLE PROPERTIES, assign a CONFIDENCE SCORE
(0–100) to each comparable based on exactly THREE factors:

  1. LOCATION SIMILARITY   — how closely the comparable's location matches the subject
  2. PROPERTY CATEGORY     — how well the project_category matches the subject's category
  3. AMENITIES             — how closely the amenity profile matches the subject

These are the ONLY three factors. Do NOT factor in developer brand, pricing, possession
status, or any other attribute. Do NOT make any external tool calls. You must perform
purely analytical scoring using the pre-extracted fields provided for each property.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCORING WEIGHTS & CRITERIA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Final score = weighted average of the three factors:

  Location similarity   → 50% weight
  Property category     → 30% weight
  Amenities             → 20% weight

1. LOCATION SIMILARITY scoring guide:
   Grade based on the provided `location_match` string and `distance_from_subject_km`:
     - "Same street"                     → 95–100
     - "Same suburb" / "Same micro-market" → 80–94 (adjust down slightly if distance > 3km)
     - "Same district" / "Same zone"     → 60–79 (adjust down if distance > 5km)
     - "Same city only"                  → 40–59 (adjust down if distance > 10km)
     - "Different city" / "Not found"    → 0–39

2. PROPERTY CATEGORY scoring guide:
   - Exact match (e.g. both "Residential")                  → 100
   - Closely related (e.g. "Villa" vs "Independent House")  → 70
   - Same broad class (e.g. both commercial subtypes)       → 50
   - Different class entirely                               → 0

3. AMENITIES scoring guide:
   Grade based on the provided `amenities` and `amenities_match` values relative to subject:
     - "Exact" / "Close" match (many common premium features) → 80–100
     - "Partial" match (some shared features, standard gym/parking) → 50–79
     - "None" / "Minimal" / "Not found"                     → 0–49

Round final score to nearest integer.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIDENCE TIERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
80–100 → "High"    |  60–79 → "Medium"
40–59  → "Low"     |   0–39 → "Very Low"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return ONLY a strict JSON array. No preamble. Start with `[`, end with `]`.

Each object MUST have exactly these keys:
{
  "project_name":          "<exact name from input>",
  "confidence_score":      <integer 0–100>,
  "confidence_tier":       "<High | Medium | Low | Very Low>",
  "confidence_reasoning":  "<3–5 sentences covering: location match found, category match result, amenities found or not found, and how the three scores combined.>",
  "research_summary": {
    "location_match":    "<Same street / Same suburb / Same district / Same city / Different city / Not found>",
    "category_match":    "<Exact / Close / Partial / None>",
    "amenities_found":   "<brief list of amenities or 'Not found'>"
  },
  "factor_breakdown": {
    "location_similarity": <0–100>,
    "property_category":   <0–100>,
    "amenities":           <0–100>
  }
}
"""

    full_comps = [
        {
            "project_name":             c.get("project_name"),
            "location":                 c.get("location"),
            "country":                  c.get("country"),
            "property_type":            c.get("property_type"),
            "project_category":         c.get("project_category"),
            "age_years":                c.get("age_years"),
            "possession_status":        c.get("possession_status"),
            "distance_from_subject_km": c.get("distance_from_subject_km"),
            "location_certainty":       c.get("location_certainty"),
            "source_url":               c.get("source_url"),
            "reason":                   c.get("reason"),
            "amenities":                c.get("amenities", "None / Not found"),
            "location_match":           c.get("location_match", "Not found"),
            "amenities_match":          c.get("amenities_match", "Not found"),
        }
        for c in batch
    ]

    user_prompt = f"""
SUBJECT PROPERTY:
{json.dumps({
    "project_name":      subject.get("project_name"),
    "location":          subject.get("location_name"),
    "country":           subject.get("country"),
    "property_type":     subject.get("property_type"),
    "project_category":  PROJECT_CATEGORY_DEFAULT.get(subject.get("property_type", ""), "Unknown"),
    "possession_status": subject.get("possession_status", "Unknown"),
    "lat":               subject.get("lat"),
    "lng":               subject.get("lng"),
}, indent=2)}

COMPARABLES TO SCORE ({len(full_comps)} properties):
{json.dumps(full_comps, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSTRUCTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For EACH comparable above:
  1. Review the provided pre-extracted `amenities`, `location_match`, `amenities_match`, and `distance_from_subject_km`.
  2. Score each of the three factors independently (location similarity, category, amenities) according to the guides.
  3. Compute final score = (location similarity × 0.5) + (category × 0.3) + (amenities × 0.2).
  4. Return one JSON object per comparable.

Do NOT make any search tool calls. Return a JSON array with exactly {len(full_comps)} objects, one per comparable.
"""
    return system_prompt, user_prompt


def _parse_confidence_json(raw: str) -> list[dict]:
    """Robust JSON parser — mirrors parse_json_safely pattern."""
    if not raw:
        return []

    def clean(s: str) -> str:
        s = s.strip()
        s = re.sub(r',\s*([\]\}])', r'\1', s)
        return s

    # 1. Markdown block
    for block in re.findall(r"```json\s*(.*?)\s*```", raw, re.DOTALL):
        try:
            data = json.loads(clean(block))
            if isinstance(data, list):
                return data
        except:
            continue

    # 2. First `[{` ... `]`
    m = re.search(r"\[\s*\{", raw)
    if m:
        depth = 0
        for i in range(m.start(), len(raw)):
            if raw[i] == "[":
                depth += 1
            elif raw[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(clean(raw[m.start(): i + 1]))
                        return data if isinstance(data, list) else []
                    except:
                        pass

    # 3. Individual object fallback
    objects = []
    for obj_str in re.findall(r"\{[^{}]+\}", raw, re.DOTALL):
        try:
            obj = json.loads(clean(obj_str))
            if "project_name" in obj and "confidence_score" in obj:
                objects.append(obj)
        except:
            continue
    if objects:
        logger.info(f"[Confidence JSON] Fallback extracted {len(objects)} objects")
    return objects


def _score_confidence_batch(subject: dict, batch: list[dict], batch_num: int) -> list[dict]:
    """
    Send one batch to gpt-4o-mini WITHOUT web_search_preview.
    The LLM will score using pre-extracted amenities and location matches.
    """
    system_prompt, user_prompt = _build_confidence_prompt(subject, batch)

    for attempt in range(1, _CONFIDENCE_RETRIES + 1):
        try:
            logger.info(
                f"[Confidence Batch {batch_num}] "
                f"Attempt {attempt}/{_CONFIDENCE_RETRIES} | "
                f"{len(batch)} comparables | web_search_preview DISABLED (pre-extracted info)"
            )

            response = _client.responses.create(
                model=_CONFIDENCE_MODEL,
                instructions=system_prompt,
                input=user_prompt,
            )

            raw    = response.output_text.strip()
            scored = _parse_confidence_json(raw)

            usage = {
                "input_tokens":  getattr(response.usage, "input_tokens", 0),
                "output_tokens": getattr(response.usage, "output_tokens", 0),
                "total_tokens":  getattr(response.usage, "total_tokens", 0),
            }
            logger.info(
                f"[Confidence Batch {batch_num}] "
                f"Scored {len(scored)}/{len(batch)} comparables | "
                f"tokens={usage['total_tokens']}"
            )

            if not scored:
                logger.warning(
                    f"[Confidence Batch {batch_num}] Zero results parsed. "
                    f"Snippet: {raw[:200]}"
                )
                if attempt < _CONFIDENCE_RETRIES:
                    time.sleep(_CONFIDENCE_DELAY)
                    continue

            return scored

        except Exception as e:
            logger.error(f"[Confidence Batch {batch_num}] Attempt {attempt} failed: {e}")
            if attempt < _CONFIDENCE_RETRIES:
                time.sleep(_CONFIDENCE_DELAY)

    return []


def _merge_confidence_scores(comparables: list[dict], scored: list[dict]) -> list[dict]:
    """Merge LLM scores back onto originals by project_name. Defaults for misses."""
    score_map = {
        (s.get("project_name") or "").lower().strip(): s
        for s in scored
    }

    for c in comparables:
        key  = (c.get("project_name") or "").lower().strip()
        data = score_map.get(key)

        if data:
            c["confidence_score"]     = data.get("confidence_score", 0)
            c["confidence_tier"]      = data.get("confidence_tier", "Unknown")
            c["confidence_reasoning"] = data.get("confidence_reasoning", "")
            c["factor_breakdown"]     = data.get("factor_breakdown", {})
            c["research_summary"]     = data.get("research_summary", {})
        else:
            logger.warning(
                f"[Confidence Merge] No score returned for '{c.get('project_name')}' "
                f"— assigning default"
            )
            c["confidence_score"]     = 50
            c["confidence_tier"]      = "Low"
            c["confidence_reasoning"] = (
                "Confidence score could not be computed by LLM for this comparable. "
                "Assigned default score of 50. Manual review recommended."
            )
            c["factor_breakdown"]  = {}
            c["research_summary"]  = {}

    return comparables


def run_confidence_scoring(
    subject: dict,
    comparables: list[dict],
    metrics=None,
    run_logger=None,
) -> list[dict]:
    """
    LLM-powered confidence scoring using pre-extracted info (tool-free, fast, cheap).

    Scores each comparable on three factors only:
      - Location similarity  (50% weight)
      - Property category    (30% weight)
      - Amenities            (20% weight)

    Enriches each comparable with:
      - confidence_score       (int 0–100)
      - confidence_tier        (High / Medium / Low / Very Low)
      - confidence_reasoning   (LLM explanation)
      - factor_breakdown       (dict: location_similarity, property_category, amenities)
      - research_summary       (location_match, category_match, amenities_found)

    Returns comparables sorted by confidence_score descending.
    """
    if not comparables:
        logger.warning("[Confidence Scoring] No comparables to score.")
        return []

    logger.info(
        f"[Confidence Scoring] Starting | "
        f"{len(comparables)} comparables | "
        f"subject='{subject.get('project_name')}' | "
        f"web_search_preview=DISABLED (pre-extracted info used) | "
        f"batch_size={_CONFIDENCE_BATCH}"
    )

    batches = [
        comparables[i: i + _CONFIDENCE_BATCH]
        for i in range(0, len(comparables), _CONFIDENCE_BATCH)
    ]
    logger.info(
        f"[Confidence Scoring] {len(batches)} batch(es) of up to {_CONFIDENCE_BATCH}"
    )

    all_scored: list[dict] = []
    for idx, batch in enumerate(batches, start=1):
        scored = _score_confidence_batch(subject, batch, batch_num=idx)
        all_scored.extend(scored)

        time.sleep(0.2)

    logger.info(
        f"[Confidence Scoring] LLM scored {len(all_scored)}/{len(comparables)} comparables"
    )

    merged = _merge_confidence_scores(comparables, all_scored)
    merged.sort(key=lambda x: x.get("confidence_score", 0), reverse=True)

    tiers = {"High": 0, "Medium": 0, "Low": 0, "Very Low": 0, "Unknown": 0}
    for c in merged:
        tier = c.get("confidence_tier", "Unknown")
        tiers[tier] = tiers.get(tier, 0) + 1
    logger.info(f"[Confidence Scoring] Done. Tier distribution: {tiers}")

    if run_logger:
        run_logger.save_step("confidence_scoring_agent", "scored_comparables", merged)

    return merged


# ══════════════════════════════════════════════════════════════════════════
# ── Scoring (legacy — kept for fallback reference) ────────────────────────
# ══════════════════════════════════════════════════════════════════════════
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

    # ── Step 1: LLM fetch ─────────────────────────────────────────────────
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

    logger.info(f"[LLM Fetch] Total raw comparables: {len(all_comps)}")

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

    # ── Step 3: Hard filter (type check) ──────────────────────────────────
    type_filtered = hard_filter_by_type(deduped, ptype)

    # ── Step 3b: Normalize project_category ───────────────────────────────
    type_filtered = stamp_project_category(type_filtered)

    logger.info(
        f"[Category] Subject category: '{subject_category}' | "
        f"Comparable categories: { {c['project_category'] for c in type_filtered} }"
    )

    if run_logger:
        run_logger.save_step("comparable_agent", "after_category_stamp", type_filtered)

    # ── Step 4: Geocode ───────────────────────────────────────────────────
    logger.info(f"[Geocode] Starting for {len(type_filtered)} comparables")
    for c in type_filtered:
        try:
            res = search_coordinates(
                location_name=c.get("location"),
                country=c.get("country"),
                project_name=c.get("project_name"),
                stage="Comparable Geocoding (S4)"
            )
            c["map_search_lat"] = res.get("lat")
            c["map_search_lng"] = res.get("lng")
            c["geocode_source"] = res.get("source")

            if "location_certainty" not in c:
                logger.warning(
                    f"[Certainty] LLM did not return location_certainty "
                    f"for '{c.get('project_name')}' — left as None"
                )

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
                    f"LLM Certainty: {c.get('location_certainty', 'None (LLM omitted)')}"
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

    # ── Step 5: Distance calculation ──────────────────────────────────────
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
        resolve_location_certainty(c)
        clean.append(c)

    logger.info(f"[Distance] {len(clean)} comparables with valid coordinates")

    # ── Step 6: Remove Subject Project ────────────────────────────────────
    filtered_no_subject = []
    subj_name = (subject.get("project_name") or "").lower().strip()
    subj_lat  = subject.get("lat")
    subj_lng  = subject.get("lng")

    def clean_name(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r'[^a-z0-9]', '', s)
        for suffix in ["society", "apartment", "apartments", "condo", "condominium",
                        "residency", "villas", "heights", "project"]:
            if s.endswith(suffix):
                s = s[:-len(suffix)]
        return s

    subj_name_clean = clean_name(subj_name)

    for c in clean:
        c_name       = (c.get("project_name") or "").lower().strip()
        c_name_clean = clean_name(c_name)
        c_lat        = c.get("map_search_lat")
        c_lng        = c.get("map_search_lng")

        coords_match = False
        if subj_lat is not None and subj_lng is not None and c_lat is not None and c_lng is not None:
            try:
                if abs(float(subj_lat) - float(c_lat)) < 1e-4 and abs(float(subj_lng) - float(c_lng)) < 1e-4:
                    coords_match = True
            except (ValueError, TypeError):
                pass

        name_match = bool(
            subj_name_clean and c_name_clean and (
                subj_name_clean == c_name_clean
                or subj_name_clean in c_name_clean
                or c_name_clean in subj_name_clean
            )
        )

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
                    "comp_name":    c_name,
                    "distance_km":  c.get("distance_from_subject_km"),
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

    # ── Step 7b: LLM Confidence Scoring (with web_search_preview) ─────────
    logger.info(
        "[Confidence] Running LLM confidence scoring with web_search_preview — "
        "LLM will research location and amenities before scoring..."
    )
    url_clean = run_confidence_scoring(
        subject=subject,
        comparables=url_clean,
        metrics=metrics,
        run_logger=run_logger,
    )

    if run_logger:
        run_logger.save_step("comparable_agent", "after_confidence_scoring", url_clean)

    # ── Step 8: Rank by confidence_score + 15km filter ───────────────────
    ranked = sorted(
        url_clean,
        key=lambda x: (
            x.get("confidence_score", 0),
            -x.get("distance_from_subject_km", 999),
        ),
        reverse=True,
    )

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
        "comparables":      nearby,
        "count":            len(nearby),
        "subject_category": subject_category,
        "_token_usage":     total_usage,
    }