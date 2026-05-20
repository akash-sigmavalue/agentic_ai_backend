from __future__ import annotations

import requests
import logging
from sqlalchemy import text
from utils.valuation.helpers import calculate_distance
from database.db import get_session

logger = logging.getLogger("amenity_analytics_tool")

AMENITY_TAGS = {
    "Healthcare": '["amenity"~"hospital|clinic"]',
    "Education": '["amenity"~"school|university|college"]',
    "Transport": '["railway"~"station|subway_entrance"]["railway"!~"platform"]',
    "Retail": '["shop"~"mall|supermarket"]',
    "Leisure": '["leisure"~"park|garden"]',
    "Transit_Bus": '["amenity"~"bus_station"]["highway"!~"bus_stop"]',
    "Transit_Stop": '["highway"~"bus_stop"]',
    "Restaurant": '["amenity"~"restaurant|cafe|fast_food|food_court|bar|pub"]',
    "Entertainment": '["amenity"~"cinema|theatre|nightclub"]',
    "Security": '["amenity"~"police|fire_station"]',
    "IT_Office": '["office"~"it|technology|coworking"]["landuse"!~"industrial"]',
}

def get_amenity_counts(amenities):
    """
    Returns a dictionary of counts per RAW amenity category.
    """
    counts = {}
    
    if not amenities:
        return counts
        
    for a in amenities:
        # Use raw category instead of mapped_type
        cat = a.get('category', 'Other')
        counts[cat] = counts.get(cat, 0) + 1
            
    # Sort keys for consistent UI display
    sorted_counts = dict(sorted(counts.items()))
    print(f"[DEBUG] get_amenity_counts (RAW) -> total: {len(amenities)}, aggregated: {sorted_counts}")
    logger.info(f"Aggregated raw counts: {sorted_counts}")
    return sorted_counts


LOCAL_AMENITY_MAP = {
    "bus_stops": "Transport",
    "metro_stations": "Transport",
    "railway_stations": "Transport",
    "schools": "Education",
    "hospitals": "Healthcare",
    "malls": "Retail",
    "it_parks": "IT_Office",
    "gardens": "Leisure",
    "restaurants_entertainment": "Restaurant",
    "police_stations": "Security",
    "fire_stations": "Security",
}

def get_local_amenities(lat, lng, city_name, radius=1000):
    """
    Fetches amenities from the PostgreSQL 'amenities' table using a spatial bounding box.
    This replaces city-name matching with direct coordinate filtering.
    """
    if lat is None or lng is None:
        return []

    # Approximate degrees per metre (1 degree lat ~= 111,111m)
    # We use a bounding box for the SQL query to keep it fast, then refine with Haversine.
    delta = radius / 111111.0
    lat_min, lat_max = lat - delta, lat + delta
    lng_min, lng_max = lng - delta, lng + delta

    session = get_session()
    try:
        # Query any amenity within the bounding box
        query = text("""
            SELECT amenity_name, amenity_category, amenity_latitude, amenity_longitude, city_name
            FROM amenities
            WHERE amenity_latitude BETWEEN :lat_min AND :lat_max
              AND amenity_longitude BETWEEN :lng_min AND :lng_max
        """)
        
        rows = session.execute(query, {
            "lat_min": lat_min, "lat_max": lat_max,
            "lng_min": lng_min, "lng_max": lng_max
        }).fetchall()

        if not rows:
            logger.debug(f"[DB] No amenities found in bounding box for {lat}, {lng}")
            return []

        logger.info(f"[DB] Found {len(rows)} candidates in spatial bounding box")

        results = []
        for row in rows:
            try:
                a_lat = float(row.amenity_latitude)
                a_lng = float(row.amenity_longitude)

                # Exact circular distance check
                dist = calculate_distance(lat, lng, a_lat, a_lng)
                if dist <= radius:
                    mapped_type = LOCAL_AMENITY_MAP.get(row.amenity_category, "Other")
                    results.append({
                        "name": row.amenity_name or "Unnamed Amenity",
                        "category": row.amenity_category,
                        "mapped_type": mapped_type,
                        "distance_m": dist,
                        "lat": a_lat,
                        "lng": a_lng,
                        "source": "PostgreSQL Database"
                    })
            except (ValueError, TypeError):
                continue

        results.sort(key=lambda x: x['distance_m'])
        return results

    except Exception as e:
        logger.error(f"Failed to fetch amenities from DB at ({lat}, {lng}): {e}")
        return []
    finally:
        session.close()

def get_nearby_amenities(lat, lng, radius=1000, city_name=None):
    """
    Fetches nearby amenities using PostgreSQL database if available, else falls back to Overpass API.
    """
    if not lat or not lng:
        return []

    # Try database first
    local_results = get_local_amenities(lat, lng, city_name, radius)
    if local_results:
        city_display = city_name.upper() if city_name else "UNKNOWN"
        logger.info(f"Found {len(local_results)} DB amenities near ({lat}, {lng})")
        print(f"[AMENITY SOURCE] ---> POSTGRESQL DATABASE ({city_display})")
        return local_results

    # Fallback to Overpass API
    print(f"[AMENITY SOURCE] ---> EXTERNAL API (Overpass OSM)")

    # Build a combined query to save API calls
    category_queries = ""
    for cat, tag in AMENITY_TAGS.items():
        category_queries += f'nwr(around:{radius},{lat},{lng}){tag};'

    query = f"""
    [out:json];
    (
      {category_queries}
    );
    out center;
    """
    
    url = "https://overpass-api.de/api/interpreter"
    headers = {
        'User-Agent': 'PropValIndiaBot/1.0',
        'Accept': 'application/json'
    }
    
    try:
        response = requests.post(url, data={'data': query}, headers=headers, timeout=20)
        if response.status_code == 429:
            logger.warning("Overpass API rate limit hit (429).")
            return []
        if response.status_code != 200:
            logger.warning(f"Overpass Amenity API error: {response.status_code} - {response.text[:200]}")
            return []
            
        data = response.json()
        elements = data.get('elements', [])
        
        results = []
        for el in elements:
            e_lat = el.get('lat') or el.get('center', {}).get('lat')
            e_lng = el.get('lon') or el.get('center', {}).get('lon')
            if not e_lat or not e_lng: continue
            
            tags = el.get('tags', {})
            name = tags.get('name') or tags.get('operator') or "Unnamed Amenity"
            
            # Determine category specific to OSM — matches all DB categories
            raw_cat = "other"
            mapped_type = "Other"

            tags_str = str(tags)
            amenity_val = tags.get('amenity', '')
            shop_val = tags.get('shop', '')
            leisure_val = tags.get('leisure', '')
            office_val = tags.get('office', '')

            if amenity_val in ('hospital',) or 'hospital' in tags_str:
                raw_cat, mapped_type = "hospitals", "Healthcare"
            elif amenity_val in ('clinic',) or 'clinic' in tags_str:
                raw_cat, mapped_type = "clinics", "Healthcare"
            elif amenity_val in ('school',) or 'school' in tags_str:
                raw_cat, mapped_type = "schools", "Education"
            elif amenity_val in ('university', 'college') or 'university' in tags_str or 'college' in tags_str:
                raw_cat, mapped_type = "colleges", "Education"
            elif 'station' in tags_str or 'subway' in tags_str:
                raw_cat, mapped_type = "metro_stations", "Transport"
            elif tags.get('highway') == 'bus_stop' or 'bus_station' in amenity_val:
                raw_cat, mapped_type = "bus_stops", "Transport"
            elif shop_val in ('mall',) or 'mall' in tags_str:
                raw_cat, mapped_type = "malls", "Retail"
            elif shop_val in ('supermarket',):
                raw_cat, mapped_type = "supermarkets", "Retail"
            elif leisure_val in ('park', 'garden') or 'park' in tags_str or 'garden' in tags_str:
                raw_cat, mapped_type = "gardens", "Leisure"
            elif amenity_val in ('restaurant', 'cafe', 'fast_food', 'food_court', 'bar', 'pub'):
                raw_cat, mapped_type = "restaurants_entertainment", "Restaurant"
            elif amenity_val in ('cinema', 'theatre', 'nightclub'):
                raw_cat, mapped_type = "restaurants_entertainment", "Entertainment"
            elif amenity_val == 'police':
                raw_cat, mapped_type = "police_stations", "Security"
            elif amenity_val == 'fire_station':
                raw_cat, mapped_type = "fire_stations", "Security"
            elif office_val in ('it', 'technology', 'coworking') or 'it park' in tags_str.lower():
                raw_cat, mapped_type = "it_parks", "IT_Office"

            dist = calculate_distance(lat, lng, e_lat, e_lng)
            
            results.append({
                "name": name,
                "category": raw_cat,
                "mapped_type": mapped_type,
                "distance_m": dist,
                "lat": e_lat,
                "lng": e_lng,
                "source": "Live OSM API"
            })
            
        # Sort by distance
        results.sort(key=lambda x: x['distance_m'])
        return results # Return all matches within radius

    except Exception as e:
        logger.error(f"OSM Amenity lookup failed: {e}")
        return []
