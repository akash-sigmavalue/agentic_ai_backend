from __future__ import annotations

import requests
import logging
import math

logger = logging.getLogger("osm_utils")

OSM_CATEGORIES = {
    'D': ['trunk', 'motorway', 'motorway_link', 'trunk_link'],
    'C': ['primary', 'primary_link', 'trunk_link'],
    'B': ['secondary', 'secondary_link'],
    'A': ['tertiary', 'residential', 'unclassified', 'service', 'living_street', 'pedestrian', 'road']
}

PRIORITY = ['D', 'C', 'B', 'A']

def get_road_category(lat, lng, radius=200):
    """
    Queries Overpass API for nearby roads and returns the highest category (D > C > B > A).
    """
    if not lat or not lng:
        return None

    # Overpass QL query: find ways with highway tag within radius
    query = f"""
    [out:json];
    (
      way(around:{radius},{lat},{lng})[highway];
    );
    out tags;
    """
    
    url = "https://overpass-api.de/api/interpreter"
    headers = {
        'User-Agent': 'PropValIndiaBot/1.0',
        'Accept': 'application/json'
    }
    
    try:
        response = requests.post(url, data={'data': query}, headers=headers, timeout=20)
        if response.status_code != 200:
            logger.warning(f"Overpass API error: {response.status_code}")
            return None
            
        data = response.json()
        highways = [element.get('tags', {}).get('highway') for element in data.get('elements', [])]
        
        found_categories = set()
        for h in highways:
            for cat, tags in OSM_CATEGORIES.items():
                if h in tags:
                    found_categories.add(cat)
                    break
        
        # Prioritize D > C > B > A
        for p in PRIORITY:
            if p in found_categories:
                return p
                
        return "A" if highways else None # Default to A if some highway found but not in our list, else None

    except Exception as e:
        logger.error(f"OSM Road Category lookup failed: {e}")
        return None


def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000 # Radius of the earth in meters
    lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c)

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
    Returns a dictionary of counts per amenity category.
    """
    counts = {
        "Healthcare": 0,
        "Education": 0,
        "Transport": 0,
        "Retail": 0,
        "Leisure": 0,
        "Restaurant": 0,
        "Entertainment": 0,
        "Security": 0,
        "IT_Office": 0,
        "Other": 0
    }
    
    if not amenities:
        return counts
        
    for a in amenities:
        cat = a.get('mapped_type', a.get('category', 'Other'))
        if cat in counts:
            counts[cat] += 1
        else:
            counts["Other"] += 1
            
    return counts


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

def get_local_amenities(lat, lng, city_name, radius=2000):
    """
    Fetches amenities from the PostgreSQL 'amenities' table, filtered by city_name.
    Uses haversine distance to return only amenities within the specified radius (metres).
    """
    if not city_name:
        return []

    try:
        from database.db import get_session
        from sqlalchemy import text
    except ImportError:
        logger.error("Database module not available for amenity lookup")
        return []

    session = get_session()
    try:
        # Query amenities for the given city (case-insensitive match)
        query = text("""
            SELECT amenity_name, amenity_category, amenity_latitude, amenity_longitude, city_name
            FROM amenities
            WHERE LOWER(city_name) = LOWER(:city)
        """)
        rows = session.execute(query, {"city": city_name.strip()}).fetchall()

        if not rows:
            # Fuzzy fallback: try partial match
            query_fuzzy = text("""
                SELECT amenity_name, amenity_category, amenity_latitude, amenity_longitude, city_name
                FROM amenities
                WHERE LOWER(city_name) LIKE :pattern
            """)
            rows = session.execute(query_fuzzy, {"pattern": f"%{city_name.strip().lower()}%"}).fetchall()

        if not rows:
            logger.debug(f"No amenities found in DB for city: {city_name}")
            return []

        logger.info(f"[DB] Fetched {len(rows)} amenity rows for city '{city_name}'")

        results = []
        for row in rows:
            try:
                a_lat = float(row.amenity_latitude)
                a_lng = float(row.amenity_longitude)

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
        logger.error(f"Failed to fetch amenities from DB for {city_name}: {e}")
        return []
    finally:
        session.close()

def get_nearby_amenities(lat, lng, radius=2000, city_name=None):
    """
    Fetches nearby amenities using Local CSVs if available, else falls back to Overpass API.
    """
    if not lat or not lng:
        return []

    # Try local data first
    if city_name:
        local_results = get_local_amenities(lat, lng, city_name, radius)
        if local_results:
            logger.info(f"Found {len(local_results)} DB amenities for {city_name}")
            print(f"[AMENITY SOURCE] ---> POSTGRESQL DATABASE ({city_name.upper()})")
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
        response = requests.post(url, data={'data': query}, headers=headers, timeout=25)
        if response.status_code != 200:
            logger.warning(f"Overpass Amenity API error: {response.status_code}")
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
