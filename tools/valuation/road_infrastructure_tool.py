import requests
import logging

logger = logging.getLogger("road_infrastructure_tool")

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
