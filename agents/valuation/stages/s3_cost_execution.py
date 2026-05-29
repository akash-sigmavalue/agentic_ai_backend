"""
Stage 3: Cost Approach Execution
=================================
Mirrors the Market Approach pipeline to derive the subject plot/land rate,
then collects cost-specific inputs from the user and applies the Cost Approach
formula to produce the final depreciated property value.

APPLICABLE PROPERTY TYPES:  villa
NOT APPLICABLE:              plot, apartment, retail, commercial_office

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
from tools.valuation.db_comparable_search import fetch_db_comparables

logger = logging.getLogger(__name__)

# Property types where Cost Approach is valid (building exists)
COST_APPLICABLE_TYPES = {"villa"}

# Area label shown in the formula audit per type
AREA_LABEL = {
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
    def execute_workflow(self, state: dict, metrics, sse_callback, run_logger=None, comparable_source: str = "web"):
        """
        Phase 1: Find comparable properties so the frontend can run the full
        market-style pipeline (listing fetch → cleaning → factorial → rate).

        After the frontend derives the subject plot/land rate, it will call the
        /cost_calculation endpoint (Phase 2) with the cost-specific inputs.

        Yields SSE events.
        """
        entities = state.get("entities", {})
        property_type = (entities.get("property_type") or "").strip().lower()

        # ── Guard: Cost Approach only applicable for villa ────────────────────
        if not self.is_applicable(property_type):
            yield sse_callback(
                "cost_not_applicable",
                {
                    "property_type": property_type,
                    "message": (
                        f"Cost Approach is not applicable for '{property_type}' properties. "
                        "It is only valid for Villa properties. Please switch to the Market Approach."
                    ),
                },
            )
            yield sse_callback("done", "Cost Approach halted — not applicable for this property type.")
            return

        # ── Step 1: Comparable Identification (mirrors Market Approach) ───────
        source_label = {
            "web":  "LLM Web Search",
            "db":   "Internal Database",
            "both": "LLM Web Search + Internal Database",
        }.get(comparable_source, "LLM Web Search")

        yield sse_callback(
            "stage",
            f"Stage 3 (Cost): Identifying comparable properties via {source_label}...",
        )

        coords = entities.get("coordinates") or {}
        subject = {
            "project_name": entities.get("project_name", "Subject Property"),
            "location_name": entities.get("location_name", ""),
            "country": entities.get("country", "India"),
            "property_type": property_type,
            "recommended_approach": "cost",
            "rate_basis": "plot_land",
            "lat": coords.get("lat") if coords.get("lat") is not None else 0,
            "lng": coords.get("lng") if coords.get("lng") is not None else 0,
        }

        all_comparables = []

        # ── Web (LLM) Source ────────────────────────────────────────────────
        if comparable_source in ("web", "both"):
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

            yield sse_callback("stage", "Stage 3a (Cost): Fetching comparables from LLM web search...")
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
            web_comps = comp_result.get("comparables", [])

            # Tag each with source
            for c in web_comps:
                c.setdefault("data_source", "Web")

            all_comparables.extend(web_comps)
            yield sse_callback("stage", f"Stage 3a (Cost) done: {len(web_comps)} comparables from web search.")

        # ── DB Source ──────────────────────────────────────────────────────
        if comparable_source in ("db", "both"):
            yield sse_callback("stage", "Stage 3b (Cost): Fetching comparables from internal database...")
            db_result = fetch_db_comparables(
                lat=subject["lat"],
                lng=subject["lng"],
                property_type=subject["property_type"],
                subject_project_name=subject["project_name"],
            )

            if db_result["status"] == "success":
                db_comps = db_result["comparables"]
                all_comparables.extend(db_comps)
                # Capture subject project from DB (if found) for listing fetch
                subject_db_project = db_result.get("subject_project")
                if subject_db_project:
                    state.setdefault("cost_data", {})["subject_db_project"] = subject_db_project
                    logger.info("[Cost Stage3b] Subject project found in DB: %s (id=%s)", subject_db_project.get("project_name"), subject_db_project.get("project_id"))
                yield sse_callback("stage", f"Stage 3b (Cost) done: {len(db_comps)} comparables from internal DB.")
            else:
                subject_db_project = None
                # No results or error — signal the UI
                yield sse_callback(
                    "db_comparable_status",
                    {
                        "status": db_result["status"],
                        "message": db_result.get("error", "No projects found in DB"),
                    },
                )
                yield sse_callback("stage", f"Stage 3b (Cost): {db_result.get('error', 'No projects found in DB')}")
        else:
            subject_db_project = None

        # Emit comparable results
        yield sse_callback(
            "comparable_results",
            {
                "comparables": all_comparables,
                "final_radius_km": None,
                "iterations": None,
                "total_found": len(all_comparables),
                "iterations_log": [],
                "comparable_source":  comparable_source,
                "subject_db_project": subject_db_project,  # subject's DB entry for listing fetch
            },
        )

        # Update state
        if "cost_data" not in state:
            state["cost_data"] = {}
        state["cost_data"]["raw_comparables"] = all_comparables
        state["cost_data"]["subject"] = subject
        if subject_db_project:
            state["cost_data"]["subject_db_project"] = subject_db_project

        # ── Inform frontend that cost-specific inputs will be needed after rate ─
        # Determine which inputs are needed based on property type
        uds_required = False
        currency = subject.get("currency") or subject.get("currency_symbol") or "₹"

        yield sse_callback(
            "cost_inputs_required",
            {
                "message": (
                    "Once the comparable pipeline derives the subject plot/land rate, "
                    "please provide the following cost-specific inputs to complete the "
                    "Cost Approach valuation."
                ),
                "property_type": property_type,
                "rate_basis": "plot_land",
                "uds_required": uds_required,
                "inputs": _build_cost_input_schema(property_type, currency=currency),
            },
        )

        yield sse_callback(
            "stage",
            "Cost Approach Phase 1 complete. Proceed with listing -> cleaning -> factorial -> plot/land rate derivation.",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # PHASE 2 — Apply Cost Approach Formula
    # ──────────────────────────────────────────────────────────────────────────
    def calculate_cost_value(
        self,
        derived_plot_rate_per_sqft: float,
        plot_area_sqft: float,
        builtup_area_sqft: float,
        property_type: str,
        construction_rate_per_sqft: float,
        age_of_property: float,
        total_life_of_building: float = DEFAULT_BUILDING_LIFE,
        currency: str = "₹",
    ) -> dict:
        """
        Apply the traditional Land + Depreciated Structure Cost Approach formula
        and return a structured result dict.

        Args:
            derived_plot_rate_per_sqft: Market-derived plot (land) rate ({currency}/sqft)
            plot_area_sqft:             Plot area of the villa in sqft (from Stage 1)
            builtup_area_sqft:          Built-up area of the villa structure in sqft (from Stage 1)
            property_type:              One of apartment | villa | retail | commercial_office
            construction_rate_per_sqft: Current construction cost per sqft ({currency}/sqft)
            age_of_property:            Completed age of the building in years (from Stage 1)
            total_life_of_building:     Expected total economic life of building in years
                                        — sourced from CPWD schedules, bank panel rates, etc.
            currency:                   The dynamic currency symbol to use for display.

        Returns:
            dict with all intermediate values + final traditional cost approach value
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

        # ── Step 1: Land Value ────────────────────────────────────────────────
        # Calculated from derived plot rate and confirmed plot area
        land_value = derived_plot_rate_per_sqft * plot_area_sqft

        # ── Step 2: Replacement Construction Cost ─────────────────────────────
        # Full replacement cost of the building component (excluding land)
        construction_cost = construction_rate_per_sqft * builtup_area_sqft

        # ── Step 3: Depreciation Rate ──────────────────────────────────────────
        # Straight-line depreciation, capped at 100%
        depreciation_rate = min(age_of_property / total_life_of_building, 1.0)

        # ── Step 4: Depreciated Building Value ────────────────────────────────
        # Remaining economic value of the building component after depreciation
        depreciated_building = construction_cost * (1.0 - depreciation_rate)

        # ── Step 5: Final Cost Approach Value ─────────────────────────────────
        # Cost Value = Land Value + Depreciated Building Value
        cost_value = land_value + depreciated_building

        return {
            "success": True,
            "property_type": property_type,
            "inputs": {
                "derived_plot_rate_per_sqft": round(derived_plot_rate_per_sqft, 2),
                "plot_area_sqft": round(plot_area_sqft, 2),
                "builtup_area_sqft": round(builtup_area_sqft, 2),
                "construction_rate_per_sqft": round(construction_rate_per_sqft, 2),
                "age_of_property": age_of_property,
                "total_life_of_building": total_life_of_building,
            },
            "calculations": {
                "land_value": round(land_value, 2),
                "construction_cost": round(construction_cost, 2),
                "depreciation_rate_pct": round(depreciation_rate * 100, 4),
                "depreciated_building_value": round(depreciated_building, 2),
            },
            "result": {
                "cost_value": round(cost_value, 2),
            },
            "formula_audit": {
                "step_1": (
                    f"Land Value = {derived_plot_rate_per_sqft} {currency}/sqft × "
                    f"{plot_area_sqft} sqft (Plot Area) = {currency}{round(land_value, 2)}"
                ),
                "step_2": (
                    f"Replacement Construction Cost = {construction_rate_per_sqft} {currency}/sqft × "
                    f"{builtup_area_sqft} sqft (Built-up Area) = {currency}{round(construction_cost, 2)}"
                ),
                "step_3": (
                    f"Depreciation = {age_of_property} yrs / {total_life_of_building} yrs "
                    f"= {round(depreciation_rate * 100, 2)}%"
                ),
                "step_4": (
                    f"Depreciated Building Value = {currency}{round(construction_cost, 2)} × "
                    f"(100% − {round(depreciation_rate * 100, 2)}%) = {currency}{round(depreciated_building, 2)}"
                ),
                "step_5": (
                    f"Cost Value = {currency}{round(land_value, 2)} (Land) + {currency}{round(depreciated_building, 2)} (Building) "
                    f"= {currency}{round(cost_value, 2)}"
                ),
            },
        }


# ──────────────────────────────────────────────────────────────────────────────
# HELPER — build the input schema emitted in cost_inputs_required SSE event
# ──────────────────────────────────────────────────────────────────────────────
def _build_cost_input_schema(property_type: str, currency: str = "₹") -> list[dict]:
    """
    Return a list of rich UI input descriptors for the cost-specific fields.
    The frontend renders these as form inputs after rate derivation.

    Villas need only 2 inputs in Phase 2 because Plot Area, Built-up Area,
    and Age are already collected as mandatory inputs during Stage 1 profiling.
    """
    return [
        {
            "field": "construction_rate_per_sqft",
            "label": "Construction Rate",
            "type": "number",
            "unit": f"{currency} / sqft",
            "required": True,
            "placeholder": "e.g. 2500",
            "help": (
                f"Current construction cost per sqft for this property type. "
                f"Applies to the subject villa's built-up area. "
                f"Refer to CPWD schedules, bank panel rates, or PWD circulars for guidance."
            ),
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
