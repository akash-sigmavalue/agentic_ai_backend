from __future__ import annotations

"""
CBD Identification Tool
=======================

Identifies the relevant Central Business Districts (CBDs) for a given set of
subject and comparable properties using a single LLM call, then geocodes each
CBD and calculates its straight-line (haversine) distance from each project.

Pipeline:
  1. Single LLM call  → identify nearby CBDs for ALL projects at once.
  2. map_search.py    → geocode each unique CBD name.
  3. geo_utils.py     → straight-line distance from project → each CBD.

Returns a per-project list of CBDs with coordinates and distance.
"""

import os
import json
import re
import logging
import time
import requests
from typing import List, Dict, Any, Optional, Tuple
from openai import OpenAI
from dotenv import load_dotenv

from tools.valuation.map_search import search_coordinates
from utils.valuation.helpers import calculate_distance

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logger = logging.getLogger("cbd_identification_tool")
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))


# ── LLM prompts ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert real estate geospatial analyst specialising in Indian cities.

Your task is to identify the most relevant Central Business Districts (CBDs) or major commercial 
micro-markets near each property given to you.

RULES:
- For each project, identify 2-4 nearby CBDs / key commercial districts.
- A "CBD" in the Indian context includes areas like BKC (Mumbai), Nariman Point (Mumbai), 
  Connaught Place (Delhi), UB City (Bengaluru), HITEC City (Hyderabad), etc.
- Include both traditional city-centre CBDs AND newer business parks / SEZs if relevant.
- Use your knowledge of Indian real estate micro-markets.
- The CBD name must be specific enough to geocode (e.g. "Bandra Kurla Complex, Mumbai" not just "BKC").
- Do NOT invent CBDs. Only include well-known, real commercial zones.

OUTPUT FORMAT (STRICT JSON — no markdown, no explanation):
{
  "results": [
    {
      "project_name": "<exact project name as given>",
      "cbds": [
        {
          "name": "<Full CBD Name, City>",
          "short_name": "<Short identifier e.g. BKC>",
          "type": "traditional_cbd" | "business_park" | "commercial_hub"
        }
      ]
    }
  ]
}
"""


def _build_user_prompt(projects: List[Dict]) -> str:
    """Build the LLM user prompt from a list of project dicts."""
    lines = []
    for p in projects:
        role = "SUBJECT" if p.get("is_subject") else "COMPARABLE"
        name = p.get("project_name", "Unknown Project")
        location = p.get("location") or p.get("location_name", "")
        country = p.get("country", "India")
        lat = p.get("lat") or p.get("map_search_lat")
        lng = p.get("lng") or p.get("map_search_lng")
        coords = f"Lat {lat}, Lng {lng}" if lat and lng else "Coordinates unknown"
        lines.append(
            f"- [{role}] {name} | Location: {location}, {country} | {coords}"
        )
    
    projects_block = "\n".join(lines)
    return f"""Identify the relevant CBDs for the following properties:

{projects_block}

Return STRICT JSON as specified. Do NOT include markdown fences."""


# ── LLM Call ─────────────────────────────────────────────────────────────────

def _llm_identify_cbds(projects: List[Dict]) -> List[Dict]:
    """
    Single LLM call that returns CBD suggestions for ALL projects at once.
    Returns parsed list of { project_name, cbds: [{name, short_name, type}] }.
    """
    user_prompt = _build_user_prompt(projects)
    model = "gpt-4o-mini"
    
    try:
        response = _client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = response.choices[0].message.content
        logger.info(f"[CBD LLM] Raw response length: {len(raw or '')}")
        
        parsed = json.loads(raw)
        results = parsed.get("results", [])
        
        usage = response.usage
        logger.info(
            f"[CBD LLM] Tokens — prompt: {usage.prompt_tokens}, "
            f"completion: {usage.completion_tokens}, total: {usage.total_tokens}"
        )
        
        return results
    
    except json.JSONDecodeError as e:
        logger.error(f"[CBD LLM] JSON parse failed: {e}")
        return []
    except Exception as e:
        logger.error(f"[CBD LLM] Call failed: {e}")
        return []


# ── Geocoding cache ───────────────────────────────────────────────────────────

_geocode_cache: Dict[str, Optional[Dict]] = {}

def _geocode_cbd(cbd_name: str, country: str = "India") -> Optional[Dict]:
    """Geocode a CBD name using map_search.py with in-memory caching."""
    cache_key = cbd_name.lower().strip()
    
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]
    
    logger.info(f"[CBD Geocode] Geocoding: '{cbd_name}'")
    result = search_coordinates(location_name=cbd_name, country=country)
    
    if "lat" in result and "lng" in result:
        _geocode_cache[cache_key] = result
        logger.info(f"[CBD Geocode] OK → ({result['lat']:.4f}, {result['lng']:.4f})")
    else:
        logger.warning(f"[CBD Geocode] Failed for '{cbd_name}': {result.get('error')}")
        _geocode_cache[cache_key] = None
    
    # Respect Nominatim rate-limits (1 req/sec) when Google Maps key is absent
    time.sleep(0.3)
    
    return _geocode_cache[cache_key]


# ── Main entry point ──────────────────────────────────────────────────────────

def identify_cbds(
    subject: Dict[str, Any],
    comparables: List[Dict[str, Any]],
) -> Dict[str, List[Dict]]:
    """
    Identify CBDs for the subject and all comparable projects.

    Parameters
    ----------
    subject : dict
        Must contain at minimum: project_name, location / location_name, country.
        Optional but recommended: lat, lng.
    comparables : list[dict]
        Same schema as subject. Each entry should have project_name, location, country.
        Optional: is_subject (bool), lat, lng.

    Returns
    -------
    dict  { project_name → [ cbd_entry, ... ] }

    Each cbd_entry:
        {
            "name"             : str   — Full CBD name
            "short_name"       : str   — Abbreviated name
            "type"             : str   — CBD type
            "lat"              : float — CBD latitude
            "lng"              : float — CBD longitude
            "geocode_source"   : str   — "google_places_api" | "nominatim" | "failed"
            "distance_m"       : int   — Straight-line distance in metres
            "distance_km"      : float — Rounded to 2 dp
        }
    """
    # Build unified project list
    all_projects: List[Dict] = [{"is_subject": True, **subject}]
    for c in comparables:
        all_projects.append({"is_subject": False, **c})
    
    logger.info(f"[CBD Tool] Identifying CBDs for {len(all_projects)} projects in a single LLM call...")
    
    # Step 1 — LLM identifies CBDs for all projects
    llm_results = _llm_identify_cbds(all_projects)
    
    if not llm_results:
        logger.warning("[CBD Tool] LLM returned no results.")
        return {}
    
    # Build a project lat/lng lookup for distance calculation
    proj_coords: Dict[str, tuple] = {}
    for p in all_projects:
        name = p.get("project_name", "")
        lat = p.get("lat") or p.get("map_search_lat")
        lng = p.get("lng") or p.get("map_search_lng")
        if name and lat and lng:
            proj_coords[name] = (float(lat), float(lng))
    
    # Step 2 & 3 — Geocode each CBD and calculate distance
    output: Dict[str, List[Dict]] = {}
    
    for proj_result in llm_results:
        pname = proj_result.get("project_name", "")
        cbds = proj_result.get("cbds", [])
        
        proj_lat, proj_lng = None, None
        if pname in proj_coords:
            proj_lat, proj_lng = proj_coords[pname]
        
        enriched_cbds = []
        for cbd in cbds:
            cbd_name = cbd.get("name", "")
            if not cbd_name:
                continue
            
            # Geocode
            geo = _geocode_cbd(cbd_name)
            
            entry: Dict[str, Any] = {
                "name": cbd_name,
                "short_name": cbd.get("short_name", ""),
                "type": cbd.get("type", "commercial_hub"),
                "lat": None,
                "lng": None,
                "geocode_source": "failed",
                "distance_m": None,
                "distance_km": None,
            }
            
            if geo:
                entry["lat"] = geo["lat"]
                entry["lng"] = geo["lng"]
                entry["geocode_source"] = geo.get("source", "unknown")
                
                # Calculate straight-line distance if project coords are known
                if proj_lat is not None and proj_lng is not None:
                    dist_m = calculate_distance(proj_lat, proj_lng, geo["lat"], geo["lng"])
                    entry["distance_m"] = dist_m
                    entry["distance_km"] = round(dist_m / 1000, 2)
            
            enriched_cbds.append(entry)
            logger.info(
                f"[CBD Tool] {pname} → {entry['short_name']} ({entry['distance_km']} km)"
                if entry["distance_km"] is not None
                else f"[CBD Tool] {pname} → {entry['short_name']} (distance unknown)"
            )
        
        # Sort by distance, unknowns last
        enriched_cbds.sort(
            key=lambda x: x["distance_km"] if x["distance_km"] is not None else 99999
        )
        output[pname] = enriched_cbds
    
    logger.info(f"[CBD Tool] Done. Results for {len(output)} projects.")
    return output


# ── OSRM Routing ─────────────────────────────────────────────────────────────

# Public OSRM demo server (driving profile, India supported)
_OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
_OSRM_TIMEOUT = 10  # seconds

def osrm_route(origin_lng: float, origin_lat: float,
               dest_lng: float, dest_lat: float) -> Optional[Dict]:
    """
    Call the public OSRM routing API.
    Returns { "distance": meters, "duration": seconds } or None on failure.
    """
    url = f"{_OSRM_BASE}/{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
    params = {"overview": "false", "steps": "false"}
    try:
        resp = requests.get(url, params=params, timeout=_OSRM_TIMEOUT,
                            headers={"User-Agent": "PropValIndiaBot/1.0"})
        if resp.status_code != 200:
            logger.warning(f"[OSRM] HTTP {resp.status_code} for ({origin_lat},{origin_lng}) → ({dest_lat},{dest_lng})")
            return None
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        route = data["routes"][0]
        return {"distance": route["distance"], "duration": route["duration"]}
    except Exception as e:
        logger.error(f"[OSRM] Request failed: {e}")
        return None


# ── CBD Scoring ───────────────────────────────────────────────────────────────

def best_cbd_for_point(
    lat: float,
    lng: float,
    cbd_candidates: List[Dict],
    w_dist: float = 0.5,
    w_time: float = 0.5,
) -> Tuple[float, Optional[Dict], List[Dict]]:
    """
    Score each candidate CBD for a project using OSRM driving distance + time.

    Parameters
    ----------
    lat, lng       : project coordinates
    cbd_candidates : list of CBD dicts from identify_cbds() — must have lat, lng
    w_dist         : weight for distance component (default 0.5)
    w_time         : weight for travel-time component (default 0.5)

    Returns
    -------
    (best_score, best_cbd_row, all_details_sorted_desc)
    """
    logger.debug(f"[CBD Score] Received weights → dist={w_dist}, time={w_time}")

    # Normalize weights safely
    try:
        w_dist = float(w_dist)
    except (ValueError, TypeError):
        w_dist = 0.5
    try:
        w_time = float(w_time)
    except (ValueError, TypeError):
        w_time = 0.5

    total_weight = w_dist + w_time
    if total_weight <= 0:
        w_dist, w_time = 0.5, 0.5
    else:
        w_dist /= total_weight
        w_time /= total_weight

    details: List[Dict] = []
    best: Optional[Dict] = None
    best_score = 0.0

    for cbd in cbd_candidates:
        cbd_lat = cbd.get("lat")
        cbd_lng = cbd.get("lng")
        if not cbd_lat or not cbd_lng:
            continue  # skip un-geocoded CBDs

        route = osrm_route(lng, lat, cbd_lng, cbd_lat)
        if not route:
            # Fallback: use straight-line distance already computed
            if cbd.get("distance_km") is not None:
                dist_km = cbd["distance_km"]
                # Estimate travel time at 30 km/h city speed
                time_min = (dist_km / 30) * 60
                logger.warning(
                    f"[CBD Score] OSRM failed for {cbd.get('short_name')} — "
                    f"falling back to straight-line estimate"
                )
            else:
                continue
        else:
            dist_km = route["distance"] / 1000
            time_min = route["duration"] / 60

        distance_component = 1 / (1 + dist_km)
        time_component = 1 / (1 + time_min)
        score = (w_dist * distance_component) + (w_time * time_component)

        row = {
            "cbd_name": cbd.get("name", ""),
            "short_name": cbd.get("short_name", ""),
            "cbd_type": cbd.get("type", "commercial_hub"),
            "lat": cbd_lat,
            "lng": cbd_lng,
            "dist_km": round(dist_km, 2),
            "time_min": round(time_min, 1),
            "w_dist": round(w_dist, 3),
            "w_time": round(w_time, 3),
            "distance_component": round(distance_component, 6),
            "time_component": round(time_component, 6),
            "score": round(score, 6),
        }
        details.append(row)

        if score > best_score:
            best_score = score
            best = row

    details.sort(key=lambda x: x["score"], reverse=True)
    logger.info(
        f"[CBD Score] Best → {best.get('short_name', 'N/A')} "
        f"score={best_score:.4f}" if best else "[CBD Score] No best found"
    )
    return round(best_score, 6), best, details
