"""
Stage 2: Workflow Agent
Orchestrate the step-by-step execution plan for the selected approach.
"""

import json

# Deterministic workflow definitions
MARKET_WORKFLOW = [
    {
        "step_number": 1,
        "step_id": "comparable_identification",
        "title": "Comparable Project Identification",
        "objective": "Identify 8-12 comparable projects within 1km of subject property",
        "executor": "llm",
        "expected_output": "List of comparable projects with rates, distances, similarity scores",
        "user_action_needed": False,
    },
    {
        "step_number": 2,
        "step_id": "radius_filter",
        "title": "1km Radius Filter",
        "objective": "Filter comparables to within 1km using Haversine formula",
        "executor": "llm",
        "expected_output": "Filtered list of comparables within 1km",
        "user_action_needed": False,
    },
    {
        "step_number": 3,
        "step_id": "rate_data_fetch",
        "title": "Transaction / Listing Data Fetch",
        "objective": "Fetch per-sqft rate for each filtered comparable",
        "executor": "llm",
        "expected_output": "Rate data with source and confidence per comparable",
        "user_action_needed": False,
    },
    {
        "step_number": 4,
        "step_id": "outlier_removal",
        "title": "Statistical Outlier Removal",
        "objective": "Remove rate outliers using IQR method",
        "executor": "llm",
        "expected_output": "Clean comparable set with median and mean rates",
        "user_action_needed": False,
    },
    {
        "step_number": 5,
        "step_id": "Factorial_table",
        "title": "Spatial Comparison & Factorial",
        "objective": "Build Factorial grid comparing each comparable to subject",
        "executor": "llm",
        "expected_output": "Factorial table with per-comparable adjusted rates",
        "user_action_needed": False,
    },
    {
        "step_number": 6,
        "step_id": "rate_derivation",
        "title": "Final Rate Derivation",
        "objective": "Derive final per-sqft rate using weighted averaging",
        "executor": "llm",
        "expected_output": "Final rate, market value, value range, confidence",
        "user_action_needed": False,
    },
]

COST_WORKFLOW = [
    {
        "step_number": 1,
        "step_id": "land_valuation",
        "title": "Land Valuation",
        "objective": "Estimate land value using market comparables or residual method",
        "executor": "llm",
        "expected_output": "Land rate per sqft and total land value",
        "user_action_needed": False,
    },
    {
        "step_number": 2,
        "step_id": "replacement_cost",
        "title": "Building Replacement Cost",
        "objective": "Estimate full replacement cost using CPWD plinth area rates",
        "executor": "llm",
        "expected_output": "Replacement cost new with location and quality factors",
        "user_action_needed": False,
    },
    {
        "step_number": 3,
        "step_id": "depreciation",
        "title": "Depreciation Calculation",
        "objective": "Apply physical, functional, and external depreciation",
        "executor": "llm",
        "expected_output": "Depreciated replacement cost with breakdown",
        "user_action_needed": False,
    },
    {
        "step_number": 4,
        "step_id": "final_cost_value",
        "title": "Final Property Value (Cost Approach)",
        "objective": "Combine land value + depreciated building value",
        "executor": "llm",
        "expected_output": "Total cost-approach value with confidence and range",
        "user_action_needed": False,
    },
]


class WorkflowAgent:
    def __init__(self):
        self.last_usage = None

    def generate_workflow(self, approach: str, entities: dict) -> list:
        """Return deterministic workflow steps based on selected approach."""
        # Reset last_usage as no LLM call is made now
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if approach.lower() == "market":
            return MARKET_WORKFLOW
        elif approach.lower() == "cost":
            return COST_WORKFLOW

        return []
