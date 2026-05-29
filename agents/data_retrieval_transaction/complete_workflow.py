from __future__ import annotations

"""
Complete Transaction Agent Workflow: Stage 1 → Stage 1.5 → Stage 2

This file orchestrates the complete flow:
1. Stage 1: Intent extraction with OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA
2. Stage 1.5: Metric verification and completion, producing FINAL_JSON_SCHEMA
3. Stage 2: Algorithm creation using the verified FINAL_JSON_SCHEMA

Usage:
    python -m agents.data_retrieval_transaction.complete_workflow "Show transactions in Baner"

Requirements:
    OPENAI_API_KEY must be available in the environment.
"""

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from agents.data_retrieval_transaction.stage1_sample import (
    TransactionStage1SampleAgent,
)
from agents.data_retrieval_transaction.stage1_5_metric_verification import (
    TransactionStage1_5SampleAgent,
)
from agents.data_retrieval_transaction.stage2_algorithm import (
    TransactionStage2SampleAgent,
)


logger = logging.getLogger(__name__)


def load_project_env() -> None:
    """Load the nearest project .env file when this sample is run directly."""
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            return


def extract_key_stage_output(events: list[dict], key: str) -> Any:
    """Extract specific event content from stage event list."""
    for event in events:
        if event.get("type") == key:
            return event.get("content")
    return None


def needs_clarification(stage1_output: dict) -> bool:
    """Check if Stage 1 needs clarification."""
    output_schema = stage1_output.get("OUTPUT_JSON_SCHEMA") or {}
    mapped_schema = stage1_output.get("MAPPED_JSON_SCHEMA") or {}
    return bool(
        stage1_output.get("needs_clarification")
        or output_schema.get("needs_clarification")
        or mapped_schema.get("needs_clarification")
    )


def run_complete_workflow(
    user_query: str,
    stage1_model: str = "gpt-4o-mini",
    stage1_5_model: str = "gpt-4o-mini",
    stage2_model: str = "gpt-4o-mini",
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    Execute complete workflow: Stage 1 → Stage 1.5 → Stage 2

    Args:
        user_query: Original user query
        stage1_model: Model for Stage 1 intent extraction
        stage1_5_model: Model for Stage 1.5 metric verification
        stage2_model: Model for Stage 2 algorithm creation
        api_key: Optional OpenAI API key

    Returns:
        Dictionary with results from all stages
    """
    load_project_env()
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv(
        "OPENAI_ADMIN_KEY"
    )
    if not resolved_api_key:
        raise RuntimeError(
            "Missing OpenAI credentials. Add OPENAI_API_KEY to the project .env file, "
            "set it in your shell, or pass --api-key when running this sample."
        )

    client = OpenAI(api_key=resolved_api_key)

    print("\n" + "=" * 80)
    print("COMPLETE TRANSACTION WORKFLOW")
    print("=" * 80)
    print(f"User Query: {user_query}\n")

    # =========================================================================
    # STAGE 1: Intent Extraction
    # =========================================================================
    print("\n" + "=" * 80)
    print("STAGE 1: INTENT EXTRACTION")
    print("=" * 80 + "\n")

    stage1_agent = TransactionStage1SampleAgent(client=client, model=stage1_model)
    stage1_events = list(stage1_agent.execute_stage1_events(user_query))

    stage1_output = extract_key_stage_output(stage1_events, "intent")
    stage1_usage = None
    for event in stage1_events:
        if event.get("type") == "token_usage_raw":
            stage1_usage = event.get("usage")

    if not stage1_output:
        raise RuntimeError("Stage 1 failed to produce intent output")

    # Check if clarification is needed
    if needs_clarification(stage1_output):
        print("\n" + "=" * 80)
        print("CLARIFICATION REQUIRED FROM USER")
        print("=" * 80)
        clarification_event = next(
            (e for e in stage1_events if e.get("type") == "clarification"),
            None,
        )
        if clarification_event:
            print(json.dumps(clarification_event.get("content"), indent=2))
        print("=" * 80 + "\n")

        return {
            "status": "clarification_needed",
            "user_query": user_query,
            "stage1_output": stage1_output,
            "message": "Please provide the missing required details and run again.",
        }

    print("✓ Stage 1 completed successfully")

    # =========================================================================
    # STAGE 1.5: Metric Verification and Completion
    # =========================================================================
    print("\n" + "=" * 80)
    print("STAGE 1.5: METRIC VERIFICATION & COMPLETION")
    print("=" * 80 + "\n")

    stage1_5_agent = TransactionStage1_5SampleAgent(
        client=client, model=stage1_5_model
    )
    stage1_5_events = list(
        stage1_5_agent.execute_stage1_5_events(user_query, stage1_output)
    )

    final_json_schema = extract_key_stage_output(stage1_5_events, "verified_intent")
    stage1_5_usage = None
    for event in stage1_5_events:
        if event.get("type") == "token_usage_raw":
            stage1_5_usage = event.get("usage")

    if not final_json_schema:
        raise RuntimeError("Stage 1.5 failed to produce verified intent")

    print("✓ Stage 1.5 completed successfully")

    # =========================================================================
    # STAGE 2: Algorithm Creation
    # =========================================================================
    print("\n" + "=" * 80)
    print("STAGE 2: ALGORITHM CREATION")
    print("=" * 80 + "\n")

    stage2_agent = TransactionStage2SampleAgent(client=client, model=stage2_model)
    stage2_events = list(
        stage2_agent.execute_stage2_events(user_query, final_json_schema)
    )

    stage2_algorithm = extract_key_stage_output(stage2_events, "algorithm")
    stage2_usage = None
    for event in stage2_events:
        if event.get("type") == "token_usage_raw":
            stage2_usage = event.get("usage")

    if not stage2_algorithm:
        raise RuntimeError("Stage 2 failed to produce algorithm")

    print("✓ Stage 2 completed successfully")

    # =========================================================================
    # Final Result
    # =========================================================================
    result = {
        "status": "success",
        "user_query": user_query,
        "final_verified_intent": final_json_schema,
        "algorithm": stage2_algorithm,
    }

    print("\n" + "=" * 80)
    print("WORKFLOW COMPLETE")
    print("=" * 80)
    print(f"Total Metrics: {len(final_json_schema.get('metrics', []))}")
    print(f"Algorithm Type: {stage2_algorithm.get('algorithm_type')}")
    print("=" * 80 + "\n")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run complete Transaction Agent workflow (Stage 1 → 1.5 → 2)."
    )
    parser.add_argument("user_query", help="Natural language transaction query")
    parser.add_argument(
        "--stage1-model",
        default="gpt-4o-mini",
        help="OpenAI model for Stage 1 intent extraction",
    )
    parser.add_argument(
        "--stage1-5-model",
        default="gpt-4o-mini",
        help="OpenAI model for Stage 1.5 metric verification",
    )
    parser.add_argument(
        "--stage2-model",
        default="gpt-4o-mini",
        help="OpenAI model for Stage 2 algorithm creation",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Optional OpenAI API key. Defaults to OPENAI_API_KEY from .env.",
    )
    args = parser.parse_args()

    result = run_complete_workflow(
        user_query=args.user_query,
        stage1_model=args.stage1_model,
        stage1_5_model=args.stage1_5_model,
        stage2_model=args.stage2_model,
        api_key=args.api_key,
    )

    print("\n" + "=" * 80)
    print("FINAL RESULT:")
    print("=" * 80)
    print(json.dumps(result, indent=2, default=str))
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
