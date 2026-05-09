"""
Map Search Tool
Allows the LLM to search for a location to fetch accurate latitude and longitude.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

def search_coordinates(location_name: str, country: str, project_name: str = None) -> dict:
    """
    Search for a location and return its latitude and longitude.
    Uses Google Maps Geocoding API if GOOGLE_MAPS_API_KEY is available,
    otherwise falls back to OpenStreetMap (Nominatim).
    """
    if not location_name or not location_name.strip():
        return {"error": "Empty location_name provided"}

    if not country or not country.strip():
        country = "India" # fallback to India if somehow missing

    # Construct the base location query
    base_location = f"{location_name}, {country}"

    # Construct a clean full query
    query = f"{project_name}, {base_location}" if project_name and project_name.strip() else base_location
    
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")

    def fetch_google_places(q):
        url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
        params = {
            "input": q,
            "inputtype": "textquery",
            "fields": "geometry",
            "key": api_key
        }
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "OK" and data.get("candidates"):
                location = data["candidates"][0]["geometry"]["location"]
                return {"lat": location["lat"], "lng": location["lng"], "source": "google_places_api"}
            return {"error": data.get("status", "NOT_FOUND")}
        return {"error": f"HTTP {response.status_code}"}

    def fetch_google_geocode(q):
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"address": q, "key": api_key}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "OK" and data.get("results"):
                location = data["results"][0]["geometry"]["location"]
                return {"lat": location["lat"], "lng": location["lng"], "source": "google_geocoding_api"}
            return {"error": data.get("status", "UNKNOWN_ERROR")}
        return {"error": f"HTTP {response.status_code}"}

    def fetch_nominatim(q):
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": q, "format": "json", "limit": 1}
        headers = {'User-Agent': 'PropValIndiaBot/1.0'}
        response = requests.get(url, params=params, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if len(data) > 0:
                return {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"]), "source": "nominatim"}
            return {"error": "Not found"}
        return {"error": f"HTTP {response.status_code}"}

    # 1. Try Google Maps Places API for POI accuracy
    if api_key:
        try:
            # Places API is vastly superior for building/project names than Geocoding API
            res = fetch_google_places(query)
            if "lat" in res: return res
            
            # If Places fails, fallback to Geocoding
            res = fetch_google_geocode(query)
            if "lat" in res: return res
            
            # Final retry with just the base location
            if project_name:
                res = fetch_google_geocode(base_location)
                if "lat" in res: return res

        except Exception as e:
            print(f"Google Maps Exception: {e}")

    # 2. Fallback to Nominatim with Project + Location + Country
    try:
        res = fetch_nominatim(query)
        if "lat" in res: return res
        # Retry with just location + country
        if project_name:
            res = fetch_nominatim(base_location)
            if "lat" in res: return res
    except Exception as e:
        return {"error": f"Exception in Nominatim: {str(e)}"}

    return {"error": "Location could not be found."}
