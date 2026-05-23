"""
PropVal India — Main Pipeline Orchestrator
Wires all agents together in a streaming SSE generator.

Flow:
  User Query -> Agent 1 (Entity Extraction) -> [Clarification if needed]
  -> Agent 2 (Approach Selection) -> [User confirms]
  -> Agent 3 (Workflow Plan) -> Execute Approach (Market or Cost)
  -> Format Final Report -> Stream via SSE
"""

import json
import time
import os
from dotenv import load_dotenv

# Load env
dotenv_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
load_dotenv(dotenv_path)

from agents.valuation.state import create_state, set_entities
from agents.valuation.stages.s1_intent import IntentExtractor
from agents.valuation.stages.s2_workflow import WorkflowAgent
from agents.valuation.stages.s3_market_execution import MarketExecutionAgent
from agents.valuation.stages.s3_cost_execution import CostExecutionAgent
from agents.valuation.tools import calculate_strategy
from agents.valuation.metrics import AgentMetrics
from utils.valuation.logging import RunLogger
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Force UTF-8
sys.stdout.reconfigure(encoding='utf-8')

def _sse(event_type: str, content, **kwargs) -> str:
    payload = {"type": event_type, "content": content, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n"


class PropertyValuationAgent:
    """
    Multi-agent property valuation pipeline.
    Implements the PropVal India SOP with SSE streaming.
    """

    def __init__(self):
        self.intent_extractor = IntentExtractor()
        self.workflow_agent = WorkflowAgent()
        self.market_executor = MarketExecutionAgent()
        self.cost_executor = CostExecutionAgent()

    def _emit_tokens(self, metrics: AgentMetrics, stage_name: str, usage, model_name="unknown"):
        """Emit token usage SSE event."""
        delta = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": model_name}
        if usage is not None:
            delta = metrics.add_tokens(usage, model_name=model_name)
        
        snap = metrics.snapshot()
        return _sse(
            "token_usage",
            {
                "stage": stage_name,
                "prompt_tokens": delta["prompt_tokens"],
                "completion_tokens": delta["completion_tokens"],
                "total_tokens": delta["total_tokens"],
                "model": delta["model"],
                "cumulative_total_tokens": snap["total_tokens"],
                "cumulative_cost_usd": snap["cost_usd"],
                "model_breakdown": snap["model_breakdown"],
                "tool_breakdown": snap["tool_breakdown"],
            },
        )



    def execute_stream(self, question: str, comparable_source: str = "web"):
        """Main streaming generator — yields SSE events for every stage."""
        metrics = AgentMetrics()
        state = create_state(question)
        state["comparable_source"] = comparable_source
        yield _sse("start", f"Processing valuation request: {question}")

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 1: Profiling (Intent, Entities, & Strategic Planning)
        # ══════════════════════════════════════════════════════════════════════
        try:
            yield _sse("stage", "Stage 1: Profiling property and strategy...")
            entities = self.intent_extractor.extract(question)
            entities["_original_query"] = question

            # Check if coordinates were extracted directly from user manual input in query
            coords = entities.get("coordinates")
            has_valid_coords = coords and coords.get("lat") is not None and coords.get("lng") is not None and coords.get("lat") != 0 and coords.get("lng") != 0
            if has_valid_coords and entities.get("coordinates_confirmed"):
                log_msg = (
                    f"\n=== [COORDINATES RESOLUTION] ===\n"
                    f"  Stage: Subject Property Profiling (S1)\n"
                    f"  Target: Project='{entities.get('project_name') or 'N/A'}', Location='{entities.get('location_name')}', Country='{entities.get('country')}'\n"
                    f"  Source: Extracted directly from user query (User manual input)\n"
                    f"  Result: Lat={coords['lat']}, Lng={coords['lng']}\n"
                    f"================================="
                )
                print(log_msg)
                logging.getLogger("map_search").info(log_msg.replace("\n", " | "))
            metrics.tools_called += 1

            token_event = self._emit_tokens(metrics, "stage1_profiling", self.intent_extractor.last_usage, model_name=self.intent_extractor.last_model)
            if token_event:
                yield token_event

            if self.intent_extractor.last_raw_response:
                yield _sse("raw_response", self.intent_extractor.last_raw_response)

            # ══════════════════════════════════════════════════════════════════════
            # DETERMINISTIC STRATEGY (Moved from LLM to Python)
            # ══════════════════════════════════════════════════════════════════════
            strategy = calculate_strategy(entities)
            
            # Update entities with strategy results
            entities["missing_mandatory"] = strategy["missing_mandatory"]
            entities["property_type_missing"] = strategy["property_type_missing"]
            entities["pt_clarification"] = strategy["pt_clarification"]
            entities["others_clarification"] = strategy["others_clarification"]
            entities["recommended_approach"] = strategy["recommended_approach"]
            
            state = set_entities(state, entities)
            yield _sse("entities", entities)

            # Initialize RunLogger now that we have a project name
            project_name = entities.get("project_name") or entities.get("location_name") or "Unnamed_Project"
            run_logger = RunLogger(project_name)
            run_logger.save_step("profiling", "entities", entities)

            approach = strategy["recommended_approach"]

            # ══════════════════════════════════════════════════════════════════════
            # THE 5-GATE SEQUENTIAL WORKFLOW (STAGE 1)
            # ══════════════════════════════════════════════════════════════════════

            # GATE 1: Property Type (Step 1)
            if strategy.get("property_type_missing"):
                yield _sse("clarification_needed", {
                    "missing_fields": ["property_type"],
                    "question": strategy.get("pt_clarification"),
                    "message": "To provide an accurate valuation, I first need to know the property type.",
                    "user_inputs_required": [s for s in strategy.get("user_inputs_required", []) if s["field"] == "property_type"]
                })
                yield _sse("stage", "Waiting for property type...")
                yield _sse("done", "Pipeline paused for property type selection.", metrics=metrics.finalize())
                return

            # GATE 2: Approach Choice logic (Step 2)
            approach = strategy["recommended_approach"]
            if strategy.get("present_choice_to_user"):
                yield _sse("approach_choice_needed", {
                    "recommended_approach": approach,
                    "alternative_approach": strategy.get("alternative_approach"),
                    "question": strategy.get("user_choice_question"),
                })
                yield _sse("stage", "Waiting for approach confirmation...")
                yield _sse("done", "Pipeline frozen waiting for decision.", metrics=metrics.finalize())
                return

            # GATE 3: Required Field Clarification (Step 3)
            # Filter fields that are NOT property_type
            other_missing = [f for f in strategy.get("missing_mandatory", []) if f != "property_type"]
            if other_missing:
                yield _sse("clarification_needed", {
                    "missing_fields": other_missing,
                    "question": strategy.get("others_clarification"),
                    "message": f"I need a few more details to complete the calculation: {', '.join(other_missing)}",
                    "user_inputs_required": [s for s in strategy.get("user_inputs_required", []) if s["field"] != "property_type"]
                })
                yield _sse("stage", "Waiting for required attributes...")
                yield _sse("done", "Pipeline paused for data clarification.", metrics=metrics.finalize())
                return

            # GATE 4: Map Confirmation logic (Step 4)
            coords = entities.get("coordinates")
            has_valid_coords = coords and coords.get("lat") is not None and coords.get("lng") is not None and coords.get("lat") != 0 and coords.get("lng") != 0
            
            if has_valid_coords and not entities.get("coordinates_confirmed"):
                yield _sse("map_confirmation", {
                    "lat": coords.get("lat"),
                    "lng": coords.get("lng"),
                    "location_name": entities.get("location_name") or entities.get("project_name") or "Subject Property",
                    "message": "I found this location on the map. Please confirm if it is correct.",
                })
                yield _sse("stage", "Waiting for map confirmation...")
                yield _sse("done", "Pipeline paused for location verification.", metrics=metrics.finalize())
                return

            # GATE 5: Extraction Verification logic (Step 5)
            if not entities.get("extraction_verified"):
                yield _sse("extraction_verification", {
                    "entities": {k: v for k, v in entities.items() if not k.startswith("_")},
                    "message": "Please review and confirm all extracted attributes before we proceed to Stage 2.",
                })
                yield _sse("stage", "Waiting for final extraction verification...")
                yield _sse("done", "Pipeline paused for data verification.", metrics=metrics.finalize())
                return

        except Exception as e:
            yield _sse("error", f"Stage 1 failed: {str(e)}")
            yield _sse("done", "", metrics=metrics.finalize())
            return

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 2: Workflow Agent — Step Plan
        # ══════════════════════════════════════════════════════════════════════
        try:
            yield _sse("stage", "Stage 2: Building execution workflow...")
            workflow_steps = self.workflow_agent.generate_workflow(approach, entities)

            token_event = self._emit_tokens(metrics, "stage2_workflow", self.workflow_agent.last_usage)
            if token_event:
                yield token_event

            yield _sse("workflow", {
                "approach": approach,
                "steps": workflow_steps,
                "total_steps": len(workflow_steps),
            })
        except Exception as e:
            yield _sse("error", f"Stage 2 failed: {str(e)}")

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 3: Approach Execution
        # ══════════════════════════════════════════════════════════════════════
        try:
            pt_lower = (entities.get("property_type") or "").strip().lower()
            if pt_lower == "plot" and approach == "cost":
                yield _sse("stage", "Cost Approach is locked for Plot properties. Switching to Market Approach.")
                approach = "market"

            if approach == "market":
                comp_source = state.get("comparable_source", "web")
                yield from self.market_executor.execute_workflow(state, metrics, _sse, run_logger=run_logger, comparable_source=comp_source)
                
                # NOTE: Stage 3 tokens are already added within comparable_selection_agent if metrics was passed.
                # But we still emit the final usage here.
                token_event = self._emit_tokens(metrics, "stage3_market", None)
                if token_event:
                    yield token_event

            elif approach == "cost":
                yield from self.cost_executor.execute_workflow(state, metrics, _sse, run_logger=run_logger)
                
                token_event = self._emit_tokens(metrics, "stage3_cost", self.cost_executor.last_usage)
                if token_event:
                    yield token_event


        except Exception as e:
            yield _sse("error", f"Stage 3 execution failed: {str(e)}")

        # ══════════════════════════════════════════════
        # PIPELINE FROZEN: STOP AFTER STAGE 3 STEP 1
        # ══════════════════════════════════════════════
        yield _sse("stage", "Pipeline frozen after comparable identification.")
        yield _sse("done", "", metrics=metrics.finalize())
        return

        #         "value_range_high_rs": results.get("value_range_high_rs"),
        #         "confidence": results.get("confidence", "medium"),
        #         "comparables_used": results.get("comparables_used"),
        #         "key_drivers": results.get("key_drivers", ""),
        #     }
        # else:
        #     final = {
        #         "approach_used": "cost",
        #         "final_value_rs": results.get("final_cost_value_rs"),
        #         "land_value_rs": results.get("land_value_rs"),
        #         "building_value_rs": results.get("depreciated_building_value_rs"),
        #         "value_range_low_rs": results.get("value_range_low_rs"),
        #         "value_range_high_rs": results.get("value_range_high_rs"),
        #         "confidence": results.get("confidence", "medium"),
        #     }
        #
        # state = set_final_valuation(state, final)
        # yield _sse("valuation_result", final)
        #
        # # ══════════════════════════════════════════════════════════════════════
        # # FORMAT REPORT
        # # ══════════════════════════════════════════════════════════════════════
        # try:
        #     yield _sse("stage", "Generating valuation report...")
        #     report = self.formatter.format_report(entities, approach, results, question)
        #
        #     token_event = self._emit_tokens(metrics, "formatter", self.formatter.last_usage)
        #     if token_event:
        #         yield token_event
        #
        #     for chunk in report.split("\n"):
        #         yield _sse("report_chunk", chunk + "\n")
        # except Exception as e:
        #     yield _sse("error", f"Report generation failed: {str(e)}")
        #
        # yield _sse("done", "", metrics=metrics.finalize())


    # ── Continue with Clarification ───────────────────────────────────────────

    def continue_with_clarification(self, session_state: dict, user_response: str):
        """
        Resume pipeline after user provides clarification.
        Re-extracts entities and continues from Stage 1.
        """
        original_query = session_state.get("raw_query", "")
        combined = f"{original_query}. {user_response}"

        # Re-run the full pipeline with enriched query
        yield from self.execute_stream(combined)

    def continue_with_approach(self, question: str, approach: str):
        """
        Resume pipeline when user selects a different approach.
        """
        # For now, just re-run with an explicit approach hint in the query
        enriched = f"{question} (Use {approach} approach)"
        yield from self.execute_stream(enriched)
