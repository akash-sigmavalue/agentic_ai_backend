"""
Stage 3: Cost Approach Execution
=================================
Mirrors the Market Approach pipeline to derive the subject property rate,
then collects cost-specific inputs from the user and applies the Cost Approach
formula to produce the final depreciated property value.

APPLICABLE PROPERTY TYPES:  apartment, villa, retail, commercial_office
NOT APPLICABLE:              plot  (no building exists — nothing to depreciate)

FORMULA:
  Construction Cost = construction_rate_per_sqft × area_sqft

    Where area_sqft is:
      - Salable / carpet area  → apartment, retail, commercial_office
      - Built-up area          → villa

  Depreciation  = age_of_property / total_life_of_building   (straight-line, capped at 1.0)

  Property Price = derived_rate_per_sqft × area_sqft         (market value from comparable pipeline)

  Cost Value = Property Price − (Construction Cost × Depreciation)

  ──────────────────────────────────────────────────────────────────────────────
  RATIONALE:
    This simplified replacement-cost approach eliminates the need for UDS,
    land rate, and net plot area — data that is often unavailable or uncertain.
    Instead, the user provides a standard construction rate per sqft (e.g.
    from CPWD schedules, bank panel rates, or PWD circulars), which is widely
    published and easy to verify.  This is the methodology used by HDFC/SBI
    valuers, DVO officers, and RICS-certified professionals.
  ──────────────────────────────────────────────────────────────────────────────
"""

import json
import logging

from tools.valuation.comparable_search import comparable_selection_agent

logger = logging.getLogger(__name__)

# Property types where Cost Approach is valid (building exists)
COST_APPLICABLE_TYPES = {"apartment", "villa", "retail", "commercial_office"}

# Area label shown in the formula audit per type
AREA_LABEL = {
    "apartment": "Salable / Carpet Area",
    "retail": "Salable Area",
    "commercial_office": "Salable Area",
    "villa": "Built-up Area",
}

# Default total life of a building (years) — user can override
DEFAULT_BUILDING_LIFE = 60


class CostExecutionAgent:
    """
    Executes the Cost Approach valuation workflow.

    Phase 1 (identical to Market Approach):
      - Comparable Identification via web search

    Phase 2 (after rate derivation on frontend):
      - Receives cost_inputs from the client
      - Applies the Cost Approach formula
      - Streams the final result

    The agent does NOT manage session state between phases.
    The calling layer (main.py / API route) is responsible for
    persisting the derived rate across the two phases.
    """

    def __init__(self):
        self.last_usage = None

    # ──────────────────────────────────────────────────────────────────────────
    # HELPER — guard: is cost approach applicable for this property type?
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def is_applicable(property_type: str) -> bool:
        """Return True if Cost Approach is valid for the given property type."""
        return (property_type or "").strip().lower() in COST_APPLICABLE_TYPES

    # ──────────────────────────────────────────────────────────────────────────
    # PHASE 1 — Comparable Identification  (same as Market Approach Step 1)
    # ──────────────────────────────────────────────────────────────────────────
    def execute_workflow(self, state: dict, metrics, sse_callback, run_logger=None):
        """
        Phase 1: Find comparable properties so the frontend can run the full
        market-style pipeline (listing fetch → cleaning → factorial → rate).

        After the frontend derives the subject property rate, it will call the
        /cost_calculation endpoint (Phase 2) with the cost-specific inputs.

        Yields SSE events.
        """
        entities = state.get("entities", {})
        property_type = (entities.get("property_type") or "").strip().lower()

        # ── Guard: Cost Approach not applicable for plots ─────────────────────
        if not self.is_applicable(property_type):
            yield sse_callback(
                "cost_not_applicable",
                {
                    "property_type": property_type,
                    "message": (
                        f"Cost Approach is not applicable for '{property_type}' properties. "
                        "It is only valid for: Apartment, Villa, Retail Shop, and "
                        "Commercial Office. Please switch to the Market Approach."
                    ),
                },
            )
            yield sse_callback("done", "Cost Approach halted — not applicable for this property type.")
            return

        # ── Step 1: Comparable Identification (mirrors Market Approach) ───────
        yield sse_callback(
            "stage",
            "Stage 3 (Cost): Identifying comparable properties via web search...",
        )

        coords = entities.get("coordinates") or {}
        subject = {
            "project_name": entities.get("project_name", "Subject Property"),
            "location_name": entities.get("location_name", ""),
            "country": entities.get("country", "India"),
            "property_type": property_type,
            "lat": coords.get("lat") if coords.get("lat") is not None else 0,
            "lng": coords.get("lng") if coords.get("lng") is not None else 0,
        }

        # Progress observer
        progress_events = []

        def on_progress(iteration, radius_km, comps_so_far, new_added):
            progress_events.append(
                {
                    "iteration": iteration,
                    "radius_km": radius_km,
                    "comps_so_far": comps_so_far,
                    "new_added": new_added,
                }
            )

        # Run comparable search
        comp_result = comparable_selection_agent(
            subject,
            on_progress=on_progress,
            run_logger=run_logger,
            metrics=metrics,
        )

        metrics.tools_called += comp_result.get("iterations", 0)

        # Emit progress events
        for p in progress_events:
            if p["new_added"] is not None:
                yield sse_callback("comparable_search_progress", p)

        # Track LLM usage
        self.last_usage = comp_result.get("_token_usage", {})

        # Emit comparable results
        comparables = comp_result.get("comparables", [])
        yield sse_callback(
            "comparable_results",
            {
                "comparables": comparables,
                "final_radius_km": comp_result.get("final_radius_km"),
                "iterations": comp_result.get("iterations"),
                "total_found": len(comparables),
                "iterations_log": comp_result.get("iterations_log", []),
            },
        )

        # Update state
        if "cost_data" not in state:
            state["cost_data"] = {}
        state["cost_data"]["raw_comparables"] = comparables
        state["cost_data"]["subject"] = subject

        # ── Inform frontend that cost-specific inputs will be needed after rate ─
        # Determine which inputs are needed based on property type
        uds_required = False

        yield sse_callback(
            "cost_inputs_required",
            {
                "message": (
                    "Once the comparable pipeline derives the subject property rate, "
                    "please provide the following cost-specific inputs to complete the "
                    "Cost Approach valuation."
                ),
                "property_type": property_type,
                "uds_required": uds_required,
                "inputs": _build_cost_input_schema(property_type),
            },
        )

        yield sse_callback(
            "stage",
            "Cost Approach Phase 1 complete. Proceed with listing → cleaning → factorial → rate derivation.",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # PHASE 2 — Apply Cost Approach Formula
    # ──────────────────────────────────────────────────────────────────────────
    def calculate_cost_value(
        self,
        derived_rate_per_sqft: float,
        area_sqft: float,
        property_type: str,
        construction_rate_per_sqft: float,
        age_of_property: float,
        total_life_of_building: float = DEFAULT_BUILDING_LIFE,
    ) -> dict:
        """
        Apply the simplified replacement-cost formula and return a structured result dict.

        Args:
            derived_rate_per_sqft:      Market-derived rate for subject property (₹/sqft)
            area_sqft:                  Salable/carpet area (apartment/retail/office) OR
                                        built-up area (villa) of the subject in sqft
            property_type:              One of apartment | villa | retail | commercial_office
            construction_rate_per_sqft: Current construction cost per sqft (₹/sqft)
                                        — sourced from CPWD schedules, bank panel rates,
                                          or PWD circulars
            age_of_property:            Completed age of the building in years
            total_life_of_building:     Expected total economic life (default 60 years)

        Returns:
            dict with all intermediate values + final cost approach value
        """
        property_type = (property_type or "").strip().lower()

        # ── Guard ──────────────────────────────────────────────────────────────
        if not self.is_applicable(property_type):
            return {
                "success": False,
                "error": f"Cost Approach is not applicable for property type '{property_type}'.",
            }

        if total_life_of_building <= 0:
            return {
                "success": False,
                "error": "total_life_of_building must be greater than 0.",
            }

        if construction_rate_per_sqft <= 0:
            return {
                "success": False,
                "error": "construction_rate_per_sqft must be greater than 0.",
            }

        area_label = AREA_LABEL.get(property_type, "Area")

        # ── Step 1: Market / Property Price ────────────────────────────────────
        # The total market value of the unit derived from the comparable pipeline
        property_price = derived_rate_per_sqft * area_sqft

        # ── Step 2: Construction Cost ──────────────────────────────────────────
        # Full replacement cost of the building component (ignores land)
        construction_cost = construction_rate_per_sqft * area_sqft

        # ── Step 3: Depreciation Rate ──────────────────────────────────────────
        # Straight-line depreciation, capped at 100%
        depreciation_rate = min(age_of_property / total_life_of_building, 1.0)

        # ── Step 4: Depreciated Construction Cost ─────────────────────────────
        # How much of the construction value has been "consumed" by age
        depreciated_construction = construction_cost * depreciation_rate

        # ── Step 5: Final Cost Approach Value ─────────────────────────────────
        # Cost Value = Market Price − (Construction Cost × Depreciation%)
        # This deducts the aged/consumed portion of the building from its market value
        cost_value = property_price - depreciated_construction

        # ── Derived per-sqft rate ──────────────────────────────────────────────
        cost_rate_per_sqft = round(cost_value / area_sqft, 2) if area_sqft > 0 else 0

        return {
            "success": True,
            "property_type": property_type,
            "area_label": area_label,
            "inputs": {
                "derived_rate_per_sqft": round(derived_rate_per_sqft, 2),
                "area_sqft": round(area_sqft, 2),
                "area_label": area_label,
                "construction_rate_per_sqft": round(construction_rate_per_sqft, 2),
                "age_of_property": age_of_property,
                "total_life_of_building": total_life_of_building,
            },
            "calculations": {
                "property_price": round(property_price, 2),
                "construction_cost": round(construction_cost, 2),
                "depreciation_rate_pct": round(depreciation_rate * 100, 4),
                "depreciated_construction_cost": round(depreciated_construction, 2),
            },
            "result": {
                "cost_value": round(cost_value, 2),
                "cost_rate_per_sqft": cost_rate_per_sqft,
            },
            "formula_audit": {
                "step_1": (
                    f"Property Price (Market) = {derived_rate_per_sqft} ₹/sqft × "
                    f"{area_sqft} sqft ({area_label}) = ₹{round(property_price, 2)}"
                ),
                "step_2": (
                    f"Construction Cost = {construction_rate_per_sqft} ₹/sqft × "
                    f"{area_sqft} sqft = ₹{round(construction_cost, 2)}"
                ),
                "step_3": (
                    f"Depreciation = {age_of_property} yrs / {total_life_of_building} yrs "
                    f"= {round(depreciation_rate * 100, 2)}%"
                ),
                "step_4": (
                    f"Depreciated Construction = ₹{round(construction_cost, 2)} × "
                    f"{round(depreciation_rate * 100, 2)}% = ₹{round(depreciated_construction, 2)}"
                ),
                "step_5": (
                    f"Cost Value = ₹{round(property_price, 2)} − ₹{round(depreciated_construction, 2)} "
                    f"= ₹{round(cost_value, 2)}"
                ),
            },
        }


# ──────────────────────────────────────────────────────────────────────────────
# HELPER — build the input schema emitted in cost_inputs_required SSE event
# ──────────────────────────────────────────────────────────────────────────────
def _build_cost_input_schema(property_type: str) -> list[dict]:
    """
    Return a list of rich UI input descriptors for the cost-specific fields.
    The frontend renders these as form inputs after rate derivation.

    New simplified approach — only 3 inputs required:
      1. construction_rate_per_sqft
      2. age_of_property
      3. total_life_of_building  (optional, default = 60)
    """
    area_hint = (
        "salable / carpet area"
        if property_type in {"apartment", "retail", "commercial_office"}
        else "built-up area"
    )

    return [
        {
            "field": "construction_rate_per_sqft",
            "label": "Construction Rate",
            "type": "number",
            "unit": "₹ / sqft",
            "required": True,
            "placeholder": "e.g. 2500",
            "help": (
                f"Current construction cost per sqft for this property type. "
                f"Applies to the {area_hint}. "
                "Refer to CPWD schedules, bank panel rates, or PWD circulars for guidance."
            ),
            "default": None,
        },
        {
            "field": "age_of_property",
            "label": "Age of Property",
            "type": "number",
            "unit": "years",
            "required": True,
            "placeholder": "e.g. 8",
            "help": "Completed age of the building in years.",
            "default": None,
        },
        {
            "field": "total_life_of_building",
            "label": "Total Life of Building",
            "type": "number",
            "unit": "years",
            "required": False,
            "placeholder": str(DEFAULT_BUILDING_LIFE),
            "help": (
                f"Expected total economic life of the building in years. "
                f"Default is {DEFAULT_BUILDING_LIFE} years (standard RBI / bank guideline)."
            ),
            "default": DEFAULT_BUILDING_LIFE,
        },
    ]
