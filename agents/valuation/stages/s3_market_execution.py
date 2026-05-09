"""
Stage 3: Market Approach Execution
Handles the step-by-step execution of the market valuation workflow.
"""

import json
from tools.valuation.comparable_search import comparable_selection_agent

class MarketExecutionAgent:
    def __init__(self):
        self.last_usage = None

    def execute_workflow(self, state: dict, metrics, sse_callback, run_logger=None):
        """
        Executes the market approach steps.
        For now, this implements Step 1 (Comparable Identification).
        """
        entities = state.get("entities", {})
        
        # Step 1: Comparable Identification
        yield sse_callback("stage", "Stage 3: Identifying comparable properties via web search...")

        # Build subject dict from Stage 1 entities
        coords = entities.get("coordinates") or {}
        subject = {
            "project_name": entities.get("project_name", "Subject Property"),
            "location_name": entities.get("location_name", ""),
            "country": entities.get("country", "India"),
            "property_type": entities.get("property_type", "apartment"),
            "lat": coords.get("lat") if coords.get("lat") is not None else 0,
            "lng": coords.get("lng") if coords.get("lng") is not None else 0,
        }

        # Progress observer
        progress_events = []
        def on_progress_sse(iteration, radius_km, comps_so_far, new_added):
            progress_events.append({
                "iteration": iteration,
                "radius_km": radius_km,
                "comps_so_far": comps_so_far,
                "new_added": new_added,
            })

        # Run Step 1
        comp_result = comparable_selection_agent(subject, on_progress=on_progress_sse, run_logger=run_logger, metrics=metrics)

        metrics.tools_called += comp_result.get("iterations", 0)

        # Emit progress events
        for p in progress_events:
            if p["new_added"] is not None:
                yield sse_callback("comparable_search_progress", p)

        # Track usage
        self.last_usage = comp_result.get("_token_usage", {})
        
        # Emit results
        comparables = comp_result.get("comparables", [])
        yield sse_callback("comparable_results", {
            "comparables": comparables,
            "final_radius_km": comp_result.get("final_radius_km"),
            "iterations": comp_result.get("iterations"),
            "total_found": len(comparables),
            "iterations_log": comp_result.get("iterations_log", []),
        })

        # Update state
        if "market_data" not in state:
            state["market_data"] = {}
        state["market_data"]["raw_comparables"] = comparables

        # TODO: Implement Steps 2-6 (Filter, Fetch, Outliers, Adjustment, Derivation)
        yield sse_callback("stage", "Pipeline frozen after comparable identification.")
                                                                            
