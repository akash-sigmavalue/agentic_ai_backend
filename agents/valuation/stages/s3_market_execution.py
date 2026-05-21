"""
Stage 3: Market Approach Execution
Handles the step-by-step execution of the market valuation workflow.
Supports three comparable sources: "web" (LLM), "db" (internal DB), or "both".
"""

import json
import logging
from tools.valuation.comparable_search import comparable_selection_agent
from tools.valuation.db_comparable_search import fetch_db_comparables

logger = logging.getLogger(__name__)


class MarketExecutionAgent:
    def __init__(self):
        self.last_usage = None

    def execute_workflow(self, state: dict, metrics, sse_callback, run_logger=None, comparable_source: str = "web"):
        """
        Executes the market approach steps.
        Implements Step 1 (Comparable Identification).

        comparable_source: "web" | "db" | "both"
        """
        entities = state.get("entities", {})

        # Step 1: Comparable Identification
        source_label = {
            "web":  "LLM Web Search",
            "db":   "Internal Database",
            "both": "LLM Web Search + Internal Database",
        }.get(comparable_source, "LLM Web Search")

        yield sse_callback("stage", f"Stage 3: Identifying comparable properties via {source_label}...")

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

        all_comparables = []

        # ── Web (LLM) Source ────────────────────────────────────────────────
        if comparable_source in ("web", "both"):
            progress_events = []

            def on_progress_sse(iteration, radius_km, comps_so_far, new_added):
                progress_events.append({
                    "iteration": iteration,
                    "radius_km": radius_km,
                    "comps_so_far": comps_so_far,
                    "new_added": new_added,
                })

            yield sse_callback("stage", "Stage 3a: Fetching comparables from LLM web search...")
            comp_result = comparable_selection_agent(
                subject,
                on_progress=on_progress_sse,
                run_logger=run_logger,
                metrics=metrics,
            )
            metrics.tools_called += comp_result.get("iterations", 0)

            for p in progress_events:
                if p["new_added"] is not None:
                    yield sse_callback("comparable_search_progress", p)

            self.last_usage = comp_result.get("_token_usage", {})
            web_comps = comp_result.get("comparables", [])

            # Tag each with source
            for c in web_comps:
                c.setdefault("data_source", "Web")

            all_comparables.extend(web_comps)
            yield sse_callback("stage", f"Stage 3a done: {len(web_comps)} comparables from web search.")

        # ── DB Source ──────────────────────────────────────────────────────
        if comparable_source in ("db", "both"):
            yield sse_callback("stage", "Stage 3b: Fetching comparables from internal database...")
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
                    state.setdefault("market_data", {})["subject_db_project"] = subject_db_project
                    logger.info("[Stage3b] Subject project found in DB: %s (id=%s)", subject_db_project.get("project_name"), subject_db_project.get("project_id"))
                yield sse_callback("stage", f"Stage 3b done: {len(db_comps)} comparables from internal DB.")
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
                yield sse_callback("stage", f"Stage 3b: {db_result.get('error', 'No projects found in DB')}")
        else:
            subject_db_project = None

        # Note: no deduplication — web and DB results are kept separately
        # so the SOURCE column and source filter remain meaningful.

        # ── Emit final results ────────────────────────────────────────────
        yield sse_callback("comparable_results", {
            "comparables":        all_comparables,
            "final_radius_km":    None,
            "iterations":         None,
            "total_found":        len(all_comparables),
            "iterations_log":     [],
            "comparable_source":  comparable_source,
            "subject_db_project": subject_db_project,  # subject's DB entry for listing fetch
        })

        # Update state
        if "market_data" not in state:
            state["market_data"] = {}
        state["market_data"]["raw_comparables"] = all_comparables

        yield sse_callback("stage", "Pipeline frozen after comparable identification.")
