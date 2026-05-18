"""
Stage 3: Cost Approach Execution
=================================
Mirrors the Market Approach pipeline to derive the subject property rate,
then collects cost-specific inputs from the user and applies the Cost Approach
formula to produce the final depreciated property value.

APPLICABLE PROPERTY TYPES:  apartment, villa, retail, commercial_office
NOT APPLICABLE:              plot  (no building exists — nothing to depreciate)

FORMULA:
  Cost Value = Property Price - (Construction Cost × Depreciation)

  Where:
    Property Price    = derived_rate_per_sqft × area_sqft
    Construction Cost = Property Price - (UDS × rate_of_plot_per_sqft)
    Depreciation      = age_of_property / total_life_of_building

  ──────────────────────────────────────────────────────────
  NOTE ON UDS:
    UDS (Undivided Share of Land) is only applicable for
    apartment / retail / commercial_office property types.
    For villas, the net_plot_area itself IS the land, so
    UDS is not separately required (it equals net_plot_area).
  ──────────────────────────────────────────────────────────
"""

import json
import logging

from tools.valuation.comparable_search import comparable_selection_agent

logger = logging.getLogger(__name__)

# Property types where Cost Approach is valid (building exists)
COST_APPLICABLE_TYPES = {"apartment", "villa", "retail", "commercial_office"}

# Property types that require a separate UDS field
UDS_REQUIRED_TYPES = {"apartment", "retail", "commercial_office"}

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
        uds_required = property_type in UDS_REQUIRED_TYPES

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
        net_plot_area_sqft: float,
        rate_of_plot_per_sqft: float,
        age_of_property: float,
        total_life_of_building: float = DEFAULT_BUILDING_LIFE,
        uds_sqft: float | None = None,
    ) -> dict:
        """
        Apply the Cost Approach formula and return a structured result dict.

        Args:
            derived_rate_per_sqft:   Market-derived rate for subject property (₹/sqft)
            area_sqft:               Carpet / built-up area of the subject (sqft)
            property_type:           One of apartment | villa | retail | commercial_office
            net_plot_area_sqft:      Net plot area in sqft
            rate_of_plot_per_sqft:   Land rate per sqft (₹/sqft)
            age_of_property:         Age of the building in years
            total_life_of_building:  Expected total life of the building (default 69 years)
            uds_sqft:                Undivided Share of Land in sqft
                                     (required for apartment / retail / commercial_office;
                                      for villa, net_plot_area_sqft is used instead)

        Returns:
            dict with all intermediate values + final cost value
        """
        property_type = (property_type or "").strip().lower()

        # ── Guard ──────────────────────────────────────────────────────────────
        if not self.is_applicable(property_type):
            return {
                "success": False,
                "error": f"Cost Approach is not applicable for property type '{property_type}'.",
            }

        # ── Step 1: Property Price (total market value of the unit) ────────────
        property_price = derived_rate_per_sqft * area_sqft

        # ── Step 2: Land cost attributed to this unit ──────────────────────────
        # For apartment / retail / commercial_office → use UDS (shared land portion)
        # For villa                                  → the whole plot is the land
        if property_type == "villa":
            effective_land_sqft = net_plot_area_sqft
        else:
            if uds_sqft is None:
                return {
                    "success": False,
                    "error": (
                        f"UDS (Undivided Share of Land) is mandatory for "
                        f"'{property_type}' properties. Please provide uds_sqft."
                    ),
                }
            effective_land_sqft = uds_sqft

        land_cost = effective_land_sqft * rate_of_plot_per_sqft

        # ── Step 3: Construction Cost ──────────────────────────────────────────
        # Construction Cost = Property Price − Land Cost
        construction_cost = property_price - land_cost

        # ── Step 4: Depreciation ───────────────────────────────────────────────
        # Depreciation = Age / Total Life  (straight-line, capped at 1.0 / 100%)
        if total_life_of_building <= 0:
            return {
                "success": False,
                "error": "total_life_of_building must be greater than 0.",
            }
        depreciation_rate = min(age_of_property / total_life_of_building, 1.0)

        # ── Step 5: Depreciated Construction Cost ─────────────────────────────
        depreciated_construction = construction_cost * depreciation_rate

        # ── Step 6: Final Cost Approach Value ─────────────────────────────────
        # Cost Value = Property Price − (Construction Cost × Depreciation)
        cost_value = property_price - depreciated_construction

        # ── Derived per-sqft rates ─────────────────────────────────────────────
        cost_rate_per_sqft = round(cost_value / area_sqft, 2) if area_sqft > 0 else 0

        return {
            "success": True,
            "property_type": property_type,
            "inputs": {
                "derived_rate_per_sqft": round(derived_rate_per_sqft, 2),
                "area_sqft": round(area_sqft, 2),
                "net_plot_area_sqft": round(net_plot_area_sqft, 2),
                "rate_of_plot_per_sqft": round(rate_of_plot_per_sqft, 2),
                "uds_sqft": round(uds_sqft, 2) if uds_sqft is not None else None,
                "effective_land_sqft": round(effective_land_sqft, 2),
                "age_of_property": age_of_property,
                "total_life_of_building": total_life_of_building,
            },
            "calculations": {
                "property_price": round(property_price, 2),
                "land_cost": round(land_cost, 2),
                "construction_cost": round(construction_cost, 2),
                "depreciation_rate_pct": round(depreciation_rate * 100, 4),
                "depreciated_construction_cost": round(depreciated_construction, 2),
            },
            "result": {
                "cost_value": round(cost_value, 2),
                "cost_rate_per_sqft": cost_rate_per_sqft,
            },
            "formula_audit": {
                "step_1": f"Property Price = {derived_rate_per_sqft} × {area_sqft} = {round(property_price, 2)}",
                "step_2": (
                    f"Land Cost = {effective_land_sqft} (UDS/Plot) × {rate_of_plot_per_sqft} = {round(land_cost, 2)}"
                ),
                "step_3": f"Construction Cost = {round(property_price, 2)} − {round(land_cost, 2)} = {round(construction_cost, 2)}",
                "step_4": f"Depreciation = {age_of_property} / {total_life_of_building} = {round(depreciation_rate * 100, 2)}%",
                "step_5": (
                    f"Depreciated Construction = {round(construction_cost, 2)} × {round(depreciation_rate, 4)} "
                    f"= {round(depreciated_construction, 2)}"
                ),
                "step_6": (
                    f"Cost Value = {round(property_price, 2)} − {round(depreciated_construction, 2)} "
                    f"= {round(cost_value, 2)}"
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
    """
    uds_required = property_type in UDS_REQUIRED_TYPES

    fields = [
        {
            "field": "net_plot_area_sqft",
            "label": "Net Plot Area",
            "type": "number",
            "unit": "sqft",
            "required": True,
            "placeholder": "e.g. 1200",
            "help": "Total plot / site area in square feet.",
            "default": None,
        },
        {
            "field": "rate_of_plot_per_sqft",
            "label": "Rate of Plot",
            "type": "number",
            "unit": "₹ / sqft",
            "required": True,
            "placeholder": "e.g. 8500",
            "help": "Current market rate of the land per square foot.",
            "default": None,
        },
    ]

    # UDS is only shown for flat / shop / office
    if uds_required:
        fields.append(
            {
                "field": "uds_sqft",
                "label": "UDS (Undivided Share of Land)",
                "type": "number",
                "unit": "sqft",
                "required": True,
                "placeholder": "e.g. 95",
                "help": (
                    "Undivided Share of Land allotted to this unit as per the "
                    "sale deed / building records (applicable for flats, shops, offices)."
                ),
                "default": None,
            }
        )

    fields += [
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
    ]

    return fields
