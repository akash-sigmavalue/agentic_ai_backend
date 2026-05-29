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
    # ── Phase 1: Rate Derivation (identical to Market Approach pipeline) ──────
    {
        "step_number": 1,
        "step_id": "comparable_identification",
        "title": "Comparable Project Identification",
        "objective": "Identify 8-12 comparable projects within 1 km of the subject property",
        "executor": "llm",
        "expected_output": "List of comparable projects with rates, distances, similarity scores",
        "user_action_needed": False,
        "phase": 1,
    },
    {
        "step_number": 2,
        "step_id": "radius_filter",
        "title": "1 km Radius Filter",
        "objective": "Filter comparables to within 1 km using Haversine formula",
        "executor": "llm",
        "expected_output": "Filtered list of comparables within 1 km",
        "user_action_needed": False,
        "phase": 1,
    },
    {
        "step_number": 3,
        "step_id": "rate_data_fetch",
        "title": "Transaction / Listing Data Fetch",
        "objective": "Fetch per-sqft rate for each filtered comparable",
        "executor": "llm",
        "expected_output": "Rate data with source and confidence per comparable",
        "user_action_needed": False,
        "phase": 1,
    },
    {
        "step_number": 4,
        "step_id": "outlier_removal",
        "title": "Statistical Outlier Removal",
        "objective": "Remove rate outliers using IQR method",
        "executor": "llm",
        "expected_output": "Clean comparable set with median and mean rates",
        "user_action_needed": False,
        "phase": 1,
    },
    {
        "step_number": 5,
        "step_id": "factorial_table",
        "title": "Spatial Comparison & Factorial",
        "objective": "Build factorial grid comparing each comparable to the subject",
        "executor": "llm",
        "expected_output": "Factorial table with per-comparable adjusted rates",
        "user_action_needed": False,
        "phase": 1,
    },
    {
        "step_number": 6,
        "step_id": "rate_derivation",
        "title": "Subject Plot/Land Rate Derivation",
        "objective": "Derive final per-sqft plot/land rate for the subject villa for use in Cost Approach",
        "executor": "llm",
        "expected_output": "Derived plot/land rate per sqft, confidence",
        "user_action_needed": False,
        "phase": 1,
    },
    # ── Phase 2: Cost Approach Calculation ────────────────────────────────────
    {
        "step_number": 7,
        "step_id": "cost_inputs_collection",
        "title": "Cost Approach Inputs",
        "objective": (
            "Collect cost-specific inputs from the user: "
            "construction rate per sqft and total building life. "
            "Subject plot area, built-up area, and age come from Stage 1 profiling."
        ),
        "executor": "user",
        "expected_output": "Validated cost input values",
        "user_action_needed": True,
        "phase": 2,
    },
    {
        "step_number": 8,
        "step_id": "cost_formula_calculation",
        "title": "Cost Approach Value Calculation",
        "objective": (
            "Apply the Cost Approach formula: "
            "Cost Value = Property Price − (Construction Cost × Depreciation). "
            "Construction Cost = Property Price − (UDS × Plot Rate). "
            "Depreciation = Age / Total Life."
        ),
        "executor": "system",
        "expected_output": "Final cost-approach value with step-by-step formula audit",
        "user_action_needed": False,
        "phase": 2,
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
