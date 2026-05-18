import sys
import os
import json
import asyncio
import logging
from typing import Any, Dict, List

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("test_tool")

# --- Sample Data ---
SAMPLE_SUBJECT = {
    "project_name": "Lodha The Park",
    "location": "Worli, Mumbai",
    "location_name": "Worli, Mumbai",
    "lat": 19.0028,
    "lng": 72.8297,
    "property_type": "apartment",
    "country": "India"
}

SAMPLE_COMPARABLES = [
    {
        "project_name": "Indiabulls Blu",
        "location": "Worli, Mumbai",
        "lat": 19.0065,
        "lng": 72.8252,
        "property_type": "apartment"
    },
    {
        "project_name": "Omkar 1973",
        "location": "Worli, Mumbai",
        "lat": 19.0015,
        "lng": 72.8260,
        "property_type": "apartment"
    }
]

def get_input(prompt: str, default: Any) -> Any:
    """Helper to get user input with a default value."""
    user_val = input(f"{prompt} [{default}]: ").strip()
    if not user_val:
        return default
    # Try to cast to same type as default
    try:
        if isinstance(default, bool):
            return user_val.lower() in ("y", "yes", "true", "1")
        if isinstance(default, int):
            return int(user_val)
        if isinstance(default, float):
            return float(user_val)
        return user_val
    except ValueError:
        print(f"Invalid input. Using default: {default}")
        return default

# --- Tool Wrappers ---

async def test_listing_search():
    from tools.valuation.listing_search import listing_pipeline
    print("\n[Tool Parameters]")
    pname = get_input("Project Name", SAMPLE_SUBJECT["project_name"])
    loc = get_input("Location", SAMPLE_SUBJECT["location"])
    lat = get_input("Latitude", SAMPLE_SUBJECT["lat"])
    lng = get_input("Longitude", SAMPLE_SUBJECT["lng"])
    ptype = get_input("Property Type", "apartment")
    max_urls = get_input("Max URLs per project", 2)
    include_comps = get_input("Include Comparables (y/n)", "n")
    
    subj = SAMPLE_SUBJECT.copy()
    subj["project_name"] = pname
    subj["location"] = loc
    subj["location_name"] = loc
    subj["lat"] = lat
    subj["lng"] = lng
    subj["property_type"] = ptype
    
    comps = []
    if include_comps.lower() == 'y':
        print("\n[Comparable Parameters]")
        cname = get_input("Comp Project Name", SAMPLE_COMPARABLES[0]["project_name"])
        cloc = get_input("Comp Location", SAMPLE_COMPARABLES[0]["location"])
        clat = get_input("Comp Latitude", SAMPLE_COMPARABLES[0]["lat"])
        clng = get_input("Comp Longitude", SAMPLE_COMPARABLES[0]["lng"])
        comps = [{
            "project_name": cname,
            "location": cloc,
            "location_name": cloc,
            "lat": clat,
            "lng": clng,
            "property_type": ptype
        }]
    
    logger.info("Testing Listing Search Tool...")
    result = listing_pipeline(
        subject=subj,
        comparables=comps,
        property_type=ptype,
        max_urls_per_project=max_urls,
        max_listings_per_project=5
    )
    return result

async def test_comparable_search():
    from tools.valuation.comparable_search import comparable_selection_agent, normalize_property_type
    print("\n[Tool Parameters]")
    pname = get_input("Subject Project Name", SAMPLE_SUBJECT["project_name"])
    loc = get_input("Location", SAMPLE_SUBJECT["location"])
    
    # Smart Defaults: If user enters Pune, suggest Pune coords
    def_lat, def_lng = SAMPLE_SUBJECT["lat"], SAMPLE_SUBJECT["lng"]
    if "pune" in loc.lower():
        def_lat, def_lng = 18.6014, 73.7744
        
    lat = get_input("Latitude", def_lat)
    lng = get_input("Longitude", def_lng)
    ptype_raw = get_input("Property Type", "apartment")
    
    ptype = normalize_property_type(ptype_raw) or ptype_raw
    
    subj = {
        "project_name": pname,
        "location_name": loc,
        "lat": lat,
        "lng": lng,
        "property_type": ptype,
        "country": "India"
    }
    
    logger.info(f"Testing Comparable Search Tool with type='{ptype}' at ({lat}, {lng})...")
    result = comparable_selection_agent(subj)
    return result

async def test_cbd_identification():
    from tools.valuation.cbd_identification_tool import identify_cbds
    print("\n[Tool Parameters]")
    pname = get_input("Subject Project Name", SAMPLE_SUBJECT["project_name"])
    lat = get_input("Latitude", SAMPLE_SUBJECT["lat"])
    lng = get_input("Longitude", SAMPLE_SUBJECT["lng"])
    
    subj = SAMPLE_SUBJECT.copy()
    subj["project_name"] = pname
    subj["lat"] = lat
    subj["lng"] = lng
    
    logger.info("Testing CBD Identification Tool...")
    result = identify_cbds(
        subject=subj,
        comparables=SAMPLE_COMPARABLES
    )
    return result

async def test_factorial_table():
    from tools.valuation.factorial_table import compute_factorial_table
    logger.info("Testing Factorial Table Tool (using sample data)...")
    result = compute_factorial_table(SAMPLE_LISTINGS, SAMPLE_SUBJECT, SAMPLE_COMPARABLES)
    return result

async def test_builtup_density():
    from tools.valuation.builtup_density_tool import analyze_congestion
    print("\n[Tool Parameters]")
    lat = get_input("Latitude", SAMPLE_SUBJECT["lat"])
    lng = get_input("Longitude", SAMPLE_SUBJECT["lng"])
    rad = get_input("Radius (meters)", 500)
    
    logger.info("Testing Builtup Density Tool...")
    result = analyze_congestion(lat, lng, rad)
    return result

async def test_amenity_analytics():
    from tools.valuation.amenity_analytics_tool import get_nearby_amenities
    print("\n[Tool Parameters]")
    lat = get_input("Latitude", SAMPLE_SUBJECT["lat"])
    lng = get_input("Longitude", SAMPLE_SUBJECT["lng"])
    rad = get_input("Radius (meters)", 1000)
    
    logger.info("Testing Amenity Analytics Tool...")
    result = get_nearby_amenities(lat, lng, radius=rad)
    return result

async def test_llm_factoring():
    from tools.valuation.llm_factoring_engine import run_llm_factoring
    from tools.valuation.factorial_table import compute_factorial_table
    
    logger.info("Generating Factorial Table for Engine input...")
    f_data = compute_factorial_table(SAMPLE_LISTINGS, SAMPLE_SUBJECT, SAMPLE_COMPARABLES)
    
    logger.info("Testing LLM Factoring Engine...")
    result = run_llm_factoring(f_data, SAMPLE_SUBJECT, SAMPLE_COMPARABLES)
    return result

async def test_map_search():
    from tools.valuation.map_search import search_coordinates
    print("\n[Tool Parameters]")
    loc = get_input("Location Name", "Worli, Mumbai")
    
    logger.info("Testing Map Search (Geocoding)...")
    result = search_coordinates(loc)
    return result

async def test_road_infra():
    from tools.valuation.road_infrastructure_tool import get_road_category
    print("\n[Tool Parameters]")
    lat = get_input("Latitude", SAMPLE_SUBJECT["lat"])
    lng = get_input("Longitude", SAMPLE_SUBJECT["lng"])
    
    logger.info("Testing Road Infrastructure Tool...")
    result = get_road_category(lat, lng)
    return result

async def test_data_cleaning():
    from tools.valuation.data_cleaning import data_cleaning_pipeline
    logger.info("Testing Data Cleaning Pipeline (using sample data)...")
    # Wrap sample raw data into the pipeline format
    raw_data = {"listings": SAMPLE_LISTINGS}
    result = data_cleaning_pipeline(raw_data, SAMPLE_SUBJECT, SAMPLE_COMPARABLES)
    return result

# Web Search Tools
async def test_web_search_enhanced():
    from tools.web_search.search import EnhancedSearcher
    print("\n[Tool Parameters]")
    query = get_input("Search Query", "ready reckoner rates Mumbai 2024")
    max_res = get_input("Max Results", 3)
    
    logger.info("Testing Enhanced Web Search...")
    searcher = EnhancedSearcher()
    result = searcher.search_with_quality(query, max_results=max_res)
    return [vars(r) for r in result]

async def test_source_discovery():
    from tools.web_search.discovery import SourceDiscovery
    print("\n[Tool Parameters]")
    query = get_input("Discovery Query", "what is the FSI for residential projects in Pune?")
    
    logger.info("Testing Source Discovery...")
    discovery = SourceDiscovery()
    result = discovery.discover(query, max_results=3)
    return result

async def test_content_processor():
    from tools.web_search.browser import ContentProcessor
    print("\n[Tool Parameters]")
    url = get_input("URL to Scrape", "https://en.wikipedia.org/wiki/Worli")
    
    logger.info("Testing Content Processor (Browser)...")
    processor = ContentProcessor()
    result = processor.process_batch([url], query="location details")
    for res in result:
        if 'extracted_data' in res:
            res['extracted_data'] = vars(res['extracted_data'])
    return result

# --- Main CLI ---

TOOLS = {
    "1": ("Listing Search", test_listing_search),
    "2": ("Comparable Search", test_comparable_search),
    "3": ("CBD Identification", test_cbd_identification),
    "4": ("Factorial Table", test_factorial_table),
    "5": ("Builtup Density", test_builtup_density),
    "6": ("Amenity Analytics", test_amenity_analytics),
    "7": ("LLM Factoring Engine", test_llm_factoring),
    "8": ("Map Search (Geocoding)", test_map_search),
    "9": ("Road Infrastructure", test_road_infra),
    "10": ("Data Cleaning Pipeline", test_data_cleaning),
    "11": ("Enhanced Web Search", test_web_search_enhanced),
    "12": ("Source Discovery", test_source_discovery),
    "13": ("Content Processor (Browser)", test_content_processor),
}

async def main():
    print("\n--- Sigmavalue OS Tool Tester ---")
    print("{:<5} {:<30}".format("ID", "Tool Name"))
    print("-" * 40)
    
    print("\n[Valuation Tools]")
    for i in range(1, 11):
        name, _ = TOOLS[str(i)]
        print("{:<5} {:<30}".format(i, name))
        
    print("\n[Web Search Tools]")
    for i in range(11, 14):
        name, _ = TOOLS[str(i)]
        print("{:<5} {:<30}".format(i, name))
        
    print("\nq. Quit")
    
    choice = input("\nSelect a tool to test (ID): ").strip().lower()
    
    if choice == 'q':
        return
    
    if choice in TOOLS:
        name, func = TOOLS[choice]
        print(f"\n--- Testing {name} ---")
        try:
            res = await func()
            print("\n[RESULT]")
            print(json.dumps(res, indent=2, default=str))
        except Exception as e:
            logger.error(f"Error testing {name}: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("Invalid choice.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
