"""
Transaction Query Builder Constants
====================================
Configuration and lookup tables for the ReAct SQL pipeline.
"""

# ══════════════════════════════════════════════════════════════════════════════
# ReAct Loop Configuration
# ══════════════════════════════════════════════════════════════════════════════

MAX_ITERATIONS: int = 5
REVIEW_SAMPLE:  int = 5

# ══════════════════════════════════════════════════════════════════════════════
# Space Filter Field Ordering & Mapping
# ══════════════════════════════════════════════════════════════════════════════

SPACE_FILTER_FIELD_ORDER: tuple[str, ...] = (
    "unit_number",
    "tower_name",
    "plot_number",
    "project_name",
    "location_name",
    "micro_market",
    "city",
    "city_name",
    "state_name",
    "country_name",
    "sub_locality",
    "village_name",
    "pincode",
)

SPACE_OPTION_TO_FIELD: dict[str, str] = {
    "unit": "unit_number",
    "building": "tower_name",
    "plot_number": "plot_number",
    "project": "project_name",
    "location": "location_name",
    "micromarket": "micro_market",
    "city": "city_name",
    "state": "state_name",
    "country": "country_name",
}
