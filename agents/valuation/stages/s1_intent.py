from __future__ import annotations

"""
Agent 1: Intent + Entity Extraction
Parse user query -> structured property entity JSON for valuation.

DESIGN PRINCIPLE:
  The LLM is fully responsible for extraction, validation, and clarification.
  NO hardcoded Python rules override the LLM output.
"""

import json
from tools.valuation.llm_caller import call_llm

SYSTEM_PROMPT = """You are a property valuation data extraction specialist for pan world.
Your ONLY task is to extract structured property entities from user queries.
Always respond with valid JSON only. No prose, no explanation."""

USER_PROMPT_TEMPLATE = """Extract all property entities from the following user query.

Query: "__USER_QUERY__"

=============================================================
PHASE 1: PROPERTY TYPE CLASSIFICATION (DO THIS FIRST)
=============================================================
The user may describe the property using ANY synonym or informal term.
Map it to EXACTLY ONE canonical property_type from this list:

| User says (examples)                             | property_type value   |
|--------------------------------------------------|-----------------------|
| flat, apartment, 2BHK, 3BHK, housing unit        | apartment             |
| villa, bungalow, row house, independent house    | villa                 |
| plot, land, NA plot, residential plot, site      | plot                  |
| shop, retail, showroom, outlet, mall unit        | retail                |
| office, commercial space, workspace, co-working  | commercial_office     |
| warehouse, godown, factory, industrial shed      | industrial            |
| farm, agricultural land, crop land, khet         | agricultural          |
| building, chawl, society building, income prop.  | building              |

RULES:
- Map any synonym or creative description to the closest canonical type above.
- If the user's term maps ambiguously between two types, prefer the more specific one.
- If it is completely unmentioned or absolutely ambiguous, you MUST set property_type to null. DO NOT default to apartment.
- Store the final canonical value in property_type.

=============================================================
PHASE 2: FIELD EXTRACTION
=============================================================
Return a JSON object with exactly this structure:
{
    "intent": "valuation",
    "project_name": "<string or null>",
    "location_name": "<string>",
    "country": "<string>",
    "currency": "<string (ISO code, e.g. INR, AED, USD)>",
    "coordinates": { "lat": <number>, "lng": <number> },
    "coordinates_confirmed": <boolean>,
    "extraction_verified": <boolean>,
    "property_type": "<apartment|villa|plot|retail|commercial_office|industrial|agricultural|building or null>",
    "carpet_area_sqft": <number or null>,
    "builtup_area_sqft": <number or null>,
    "plot_area_sqft": <number or null>,

    "configuration": "<string or null>",
    "total_floors": <integer or null>,
    "subject_floor": <integer or null>,
    "age_years": <number or null>,

    "facing": "<string or null>",
    "amenities": [<array or empty array>],

    "construction_quality": "<standard|premium|luxury or null>",
    "structural_type": "<rcc_framed|load_bearing|steel or null>",

    "land_type": "<agricultural|non_agricultural|residential|commercial or null>",
    "zoning": "<residential|commercial|industrial|mixed or null>",
    "road_access": "<yes|no or null>",
    "frontage": "<number or null>",

    "floor_preference": "<ground|upper|any or null>",
    "visibility": "<high|medium|low or null>",
    "footfall": "<high|medium|low or null>",

    "tenant_profile": "<string or null>",
    "rental_income": <number or null>,
    "occupancy_status": "<vacant|leased|self_use or null>",

    "clear_height": <number or null>,
    "power_supply": "<adequate|high|low or null>",

    "soil_type": "<string or null>",
    "water_availability": "<good|moderate|poor or null>",

    "land_ownership": "<freehold|leasehold or null>",
    "maintenance_condition": "<good|average|poor or null>",
    "neighbourhood_trend": "<improving|stable|declining or null>",

    "user_requested_approach": "<market|cost|null>"
}

=============================================================
EXTRACTION RULES
=============================================================
coordinates:
  - NEVER guess or hallucinate coordinates.
  - IF the user explicitly provides coordinates (e.g. "18.55, 73.79" or "lat 18.55, lng 73.79") in the query, you MUST use them directly. DO NOT call the `search_map` tool in this case.
  - IF coordinates are NOT provided, you MUST use the `search_map` tool to search for the location using the `project_name`, `location_name`, and `country`.
  - Wait for the tool to return the real `lat` and `lng`, then populate the `coordinates` field.
  - If the tool fails to find the location, leave the coordinates as null and do NOT guess them.

coordinates_confirmed:
  - Set to true IF the user explicitly says the map location is correct OR if the user provides the correct coordinates manually.
  - Otherwise, set to false.

extraction_verified:
  - Set to true IF the user explicitly states that all extracted property details are correct, verified, or confirmed.
  - Set to true IF the user explicitly provides corrections to the extracted details (e.g. "The extracted details are confirmed with the following corrections: ...").
  - Otherwise, set to false.

country:
  - ALWAYS extract the exact country name if stated. 
  - If missing, intelligently GUESS the country based on the location or project name (e.g., if you see Pune, guess India).

currency:
  - Extract the 3-letter ISO currency code for the project location (e.g., "INR" for India, "AED" for UAE/Dubai, "USD" for USA).
  - If missing, intelligently GUESS the currency based on the country or location.

location_name:
  - Extract the Locality, Area, or City (e.g., "Baner", "Sus", "Pune").
  - If missing, intelligently GUESS the locality or city based on the project name or context.
  - If `location_name` is STILL completely unknown and cannot be guessed, leave it as null.

area values:
  - NEVER guess carpet_area_sqft, plot_area_sqft, or builtup_area_sqft — keep null if not stated.
  - Convert sqm -> sqft (multiply by 10.764).

user_requested_approach:
  - Extract IF AND ONLY IF the user explicitly phrases a demand for the "market approach" or "cost approach" (e.g. "Proceed with the cost approach" or "Use market approach").
  - DO NOT extract this if the user simply asks "What is the market value" or "Find the market price". "Market value" is a common phrase and does NOT mean the user selected the Market Approach.
  - If the user demands any other approach (like income or dcf), keep as null.
  - Otherwise leave it null. Do NOT guess.


"""

from tools.valuation.map_search import search_coordinates

class IntentExtractor:
    def __init__(self):
        self.last_usage = None
        self.last_raw_response = None
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_map",
                    "description": "Searches for a location (like an address, city, or project name) on the map and returns real latitude and longitude.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location_name": {
                                "type": "string",
                                "description": "The general area or city provided, e.g. 'Sus', 'Baner', 'Pune'"
                            },
                            "project_name": {
                                "type": "string",
                                "description": "The specific building or project name, e.g. 'Om Paradise'. Leave empty if not stated."
                            },
                            "country": {
                                "type": "string",
                                "description": "The country to restrict the search to, e.g. 'India', 'USA'."
                            }
                        },
                        "required": ["location_name", "country"]
                    }
                }
            }
        ]
        self.tool_functions = {"search_map": search_coordinates}

    def extract(self, user_query: str) -> dict:
        """Extract property entities from natural language query."""
        user_prompt = USER_PROMPT_TEMPLATE.replace("__USER_QUERY__", user_query)
        result = call_llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=2000,
            temperature=0.1,
            tools=self.tools,
            tool_functions=self.tool_functions
        )

        self.last_usage = result.get("usage")
        self.last_model = result.get("model", "gpt-4o-mini")
        self.last_raw_response = result.get("raw")


        if result["success"]:
            entities = result["data"]
            # The LLM is now fully responsible for validation and clarification
            # based on the mandatory logic provided in the prompt.
            return entities

        # Fallback on failure
        return {
            "intent": "valuation",
            "project_name": None,
            "location_name": None,
            "coordinates": {"lat": 0, "lng": 0},
            "coordinates_confirmed": False,
            "extraction_verified": False,
            "property_type": None,
            "carpet_area_sqft": None,
            "user_requested_approach": None,
            "missing_mandatory": ["location_name", "carpet_area_sqft", "age_years"],
            "clarification_needed": "I could not understand the property details. Please provide at least the location, carpet area, and age of the property.",
        }

    def merge_clarification(self, existing_entities: dict, user_response: str) -> dict:
        """Re-extract entities by combining original query with user's clarification response."""
        original_query = existing_entities.get("_original_query", "")
        combined_query = f"{original_query}. Additional details: {user_response}"
        return self.extract(combined_query)
