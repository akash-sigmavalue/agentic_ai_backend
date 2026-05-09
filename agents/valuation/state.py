"""
ValuationState — Central state object that flows through all agents.
"""

import uuid
from datetime import datetime, timezone


def create_state(raw_query: str) -> dict:
    """Create a fresh ValuationState for a new session."""
    return {
        "session_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_query": raw_query,

        # Agent 1 output
        "entities": None,

        # Agent 2 output
        "approach": None,
        "approach_confidence": None,
        "approach_justification": None,
        "alternative_approach": None,
        "user_inputs_required": [],
        "user_inputs_collected": {},

        # Agent 3 output
        "workflow_steps": [],

        # Market Approach outputs
        "market_data": {
            "raw_comparables": [],
            "filtered_comparables": [],
            "rate_data": [],
            "clean_comparables": [],
            "comparison_table": [],
            "final_rate_rs_sqft": None,
            "final_market_value_rs": None,
        },

        # Cost Approach outputs
        "cost_data": {
            "land_value_rs": None,
            "land_method": None,
            "replacement_cost_new_rs": None,
            "depreciated_building_value_rs": None,
            "final_cost_value_rs": None,
        },

        # Final reconciled output
        "final_valuation": None,
    }


def set_entities(state: dict, entities: dict) -> dict:
    state["entities"] = entities
    return state


def set_approach(state: dict, approach_data: dict) -> dict:
    state["approach"] = approach_data.get("recommended_approach")
    state["approach_confidence"] = approach_data.get("confidence")
    state["approach_justification"] = approach_data.get("justification")
    state["alternative_approach"] = approach_data.get("alternative_approach")
    return state


def set_workflow(state: dict, steps: list) -> dict:
    state["workflow_steps"] = steps
    return state


def update_market_data(state: dict, key: str, value) -> dict:
    state["market_data"][key] = value
    return state


def update_cost_data(state: dict, key: str, value) -> dict:
    state["cost_data"][key] = value
    return state


def set_final_valuation(state: dict, valuation: dict) -> dict:
    state["final_valuation"] = valuation
    return state
