"""
PropVal India — Valuation Strategy Engine
Deterministic logic for approach selection and input validation.
"""

# Mandatory fields by property type (Independent of approach)
MANDATORY_FIELDS_BY_TYPE = {
    # project_name is mandatory for built/named properties (apartment, villa, retail, office)
    # It is intentionally EXCLUDED for plot — plots don't belong to named projects.
    "apartment": ["project_name", "location_name", "country", "property_type", "salable_area_sqft", "age_years"],
    "villa": ["project_name", "location_name", "country", "property_type", "plot_area_sqft", "builtup_area_sqft", "age_years"],
    "plot": ["location_name", "country", "property_type", "plot_area_sqft", "land_type"],
    "retail": ["project_name", "location_name", "country", "property_type", "salable_area_sqft", "frontage"],
    "commercial_office": ["project_name", "location_name", "country", "property_type", "salable_area_sqft", "occupancy_status"],
    "industrial": ["location_name", "country", "property_type", "plot_area_sqft", "builtup_area_sqft", "clear_height"],
    "agricultural": ["location_name", "country", "property_type", "plot_area_sqft", "water_availability"],
    "building": ["location_name", "country", "property_type", "plot_area_sqft", "builtup_area_sqft", "age_years", "occupancy_status"],
}

# Fields required by approach at Stage-1 time.
# NOTE: Cost Approach phase-2 inputs (net_plot_area, rate_of_plot, UDS, age,
# building_life) are collected AFTER rate derivation — NOT here.
APPROACH_REQUIREMENTS = {
    "market": [],
    "cost": [],       # cost-specific inputs are collected post rate-derivation
}

# Friendly names for fields used in clarification questions
FIELD_LABELS = {
    "project_name": "project / society name",
    "location_name": "location",
    "country": "country",
    "property_type": "property type",
    "carpet_area_sqft": "carpet area",
    "salable_area_sqft": "salable area",
    "builtup_area_sqft": "built-up area",
    "plot_area_sqft": "plot area",
    "age_years": "age of the building",
    "land_type": "land type",
    "frontage": "road frontage",
    "occupancy_status": "occupancy status",
    "clear_height": "clear height", 
    "water_availability": "water availability",
    "land_ownership": "land ownership",
    "construction_quality": "quality of construction",
}

# Rich UI schemas for fields that need dropdowns
FIELD_SCHEMAS = {
    "project_name": {
        "field": "project_name",
        "label": "Project / Society Name",
        "type": "text",
        "required": True,
        "placeholder": "e.g. Godrej Infinity, Lodha Altamount",
        "default": None
    },
    "property_type": {
        "field": "property_type",
        "label": "Property Type",
        "type": "select",
        "required": True,
        "options": [
            {"value": "apartment", "label": "Apartment (Flat / Housing Unit)"},
            {"value": "villa", "label": "Villa (Bungalow / Row House)"},
            {"value": "plot", "label": "Plot (Land / NA Plot)"},
            {"value": "retail", "label": "Retail (Shop / Showroom)"},
            {"value": "commercial_office", "label": "Commercial Office (Workspace)"}
        ],
        "default": None
    },
    "construction_quality": {
        "field": "construction_quality",
        "label": "Construction Quality",
        "type": "select",
        "required": True,
        "options": ["standard", "premium", "luxury"],
        "default": None
    },
    "land_ownership": {
        "field": "land_ownership",
        "label": "Land Ownership",
        "type": "select",
        "required": True,
        "options": ["freehold", "leasehold"],
        "default": None
    },
    "land_type": {
        "field": "land_type",
        "label": "Land Type",
        "type": "select",
        "required": True,
        "options": ["agricultural", "non_agricultural", "residential", "commercial"],
        "default": None
    },
    "occupancy_status": {
        "field": "occupancy_status",
        "label": "Occupancy Status",
        "type": "select",
        "required": True,
        "options": ["vacant", "leased", "self_use"],
        "default": None
    },
    "water_availability": {
        "field": "water_availability",
        "label": "Water Availability",
        "type": "select",
        "required": True,
        "options": ["good", "moderate", "poor"],
        "default": None
    }
}


# Property types where Cost Approach is valid (building exists to depreciate)
_COST_APPLICABLE_TYPES = {"apartment", "villa", "retail", "commercial_office"}


def calculate_strategy(entities: dict) -> dict:
    """
    Deterministic logic to decide:
    1. Recommended approach (Market or Cost, based on user request & property type)
    2. Missing mandatory fields (Separated by property_type vs others)
    3. Clarification questions
    """
    property_type = entities.get("property_type")
    user_requested = entities.get("user_requested_approach")
    pt_lower = (property_type or "").strip().lower()

    # 1. Recommended Approach selection
    if user_requested == "cost":
        if pt_lower == "plot":
            # Cost Approach is NOT applicable for plots — override to market
            recommended = "market"
            justification = (
                "Cost Approach is not applicable for plot properties (no building to depreciate). "
                "Switching to the Market Approach automatically."
            )
        elif pt_lower in _COST_APPLICABLE_TYPES:
            recommended = "cost"
            justification = (
                f"Cost Approach selected as requested. "
                f"Applicable for {pt_lower} properties."
            )
        else:
            # Unknown / unsupported type — default to market
            recommended = "market"
            justification = (
                f"Cost Approach is not supported for '{pt_lower}'. "
                "Defaulting to the Market Approach."
            )
    elif user_requested and user_requested != "cost":
        recommended = user_requested
        justification = f"The {user_requested.title()} Approach was selected based on your specific request."
    else:
        recommended = "market"
        type_str = pt_lower if pt_lower else "property"
        justification = f"The Market Approach is the recommended methodology for {type_str}s based on comparable data."

    # 2. Identify missing mandatory fields
    missing_fields = []
    property_type_missing = False
    
    if not property_type:
        property_type_missing = True
        missing_fields.append("property_type")
    else:
        # Check type-specific mandatory fields
        type_mandatory = MANDATORY_FIELDS_BY_TYPE.get(property_type, [])
        for field in type_mandatory:
            val = entities.get(field)
            if val is None or val == "":
                missing_fields.append(field)
            
    # Check approach-specific requirements
    approach_reqs = APPROACH_REQUIREMENTS.get(recommended, [])
    for field in approach_reqs:
        val = entities.get(field)
        if (val is None or val == "") and field not in missing_fields:
            missing_fields.append(field)
            
    # 3. Generate natural clarification questions
    # Question 1: If property type is missing
    pt_clarification = "To start, what is the property type? (e.g. Apartment, Villa, Plot, Commercial Office)" if property_type_missing else None
    
    # Question 2: If other fields are missing
    other_fields = [f for f in missing_fields if f != "property_type"]
    others_clarification = None
    if other_fields:
        labels = [FIELD_LABELS.get(f, f.replace("_", " ")) for f in other_fields]
        if len(labels) == 1:
            others_clarification = f"I need a bit more information to proceed: could you tell me the {labels[0]} of the property?"
        elif len(labels) == 2:
            others_clarification = f"I'm missing some details: could you provide the {labels[0]} and {labels[1]}?"
        else:
            list_str = ", ".join(labels[:-1]) + f", and {labels[-1]}"
            others_clarification = f"To give you an accurate valuation, I need a few more details: {list_str}."

    # 4. Handle approach choice logic
    present_choice = False
    if not user_requested and not property_type_missing and pt_lower != "plot":
        present_choice = True
        
    # 5. Build rich schemas for missing fields UI
    user_inputs_required = []
    for field in missing_fields:
        if field in FIELD_SCHEMAS:
            user_inputs_required.append(FIELD_SCHEMAS[field])
        else:
            # Fallback for text/number fields
            user_inputs_required.append({
                "field": field,
                "label": FIELD_LABELS.get(field, field.replace("_", " ").title()),
                "type": "number" if ("sqft" in field or "years" in field or "frontage" in field or "height" in field) else "text",
                "required": True,
                "default": None
            })

    return {
        "recommended_approach": recommended,
        "approach_justification": justification,
        "alternative_approach": None if pt_lower == "plot" else ("market" if recommended == "cost" else "cost"),
        "present_choice_to_user": present_choice,
        "user_choice_question": (
            f"I recommend the {recommended.title()} Approach for this valuation."
            if pt_lower == "plot" else
            f"I recommend the {recommended.title()} Approach for this valuation. Would you like to proceed with this, or switch to the {('Market' if recommended == 'cost' else 'Cost')} Approach?"
        ),
        "property_type_missing": property_type_missing,
        "pt_clarification": pt_clarification,
        "missing_mandatory": missing_fields,
        "others_clarification": others_clarification,
        "user_inputs_required": user_inputs_required,
    }
