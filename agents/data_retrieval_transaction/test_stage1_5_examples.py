"""
Example script demonstrating Stage 1.5 Metric Verification workflow.

This script shows different ways to use Stage 1.5 and how to handle the outputs.

Run this with:
    python -m agents.data_retrieval_transaction.test_stage1_5_examples

Or from the root directory:
    python agents/data_retrieval_transaction/test_stage1_5_examples.py
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from agents.data_retrieval_transaction.complete_workflow import run_complete_workflow
from agents.data_retrieval_transaction.stage1_5_metric_verification import (
    TransactionStage1_5SampleAgent,
)
from agents.data_retrieval_transaction.stage1_sample import TransactionStage1SampleAgent


def load_env():
    """Load environment variables."""
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            return


def example_1_complete_workflow():
    """Example 1: Run complete workflow end-to-end."""
    print("\n" + "=" * 80)
    print("EXAMPLE 1: COMPLETE WORKFLOW (Stage 1 → 1.5 → 2)")
    print("=" * 80 + "\n")

    query = "Show total sales value and transaction count in Baner for 2024"
    print(f"Query: {query}\n")

    result = run_complete_workflow(query)

    # Extract final schema
    final_schema = result["final_verified_intent"]
    print(f"\n✓ Workflow completed successfully")
    print(f"  - Metrics: {len(final_schema.get('metrics', []))}")
    print(f"\nFinal Verified Metrics:")
    for metric in final_schema.get("metrics", []):
        print(f"  - {metric.get('name')}: {metric.get('description')}")


def example_2_manual_pipeline():
    """Example 2: Manual stage-by-stage execution."""
    print("\n" + "=" * 80)
    print("EXAMPLE 2: MANUAL PIPELINE EXECUTION")
    print("=" * 80 + "\n")

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY")
    if not api_key:
        print("❌ No API key found. Set OPENAI_API_KEY or OPENAI_ADMIN_KEY")
        return

    client = OpenAI(api_key=api_key)
    user_query = "Get average price and total transactions for residential units in Mumbai 2024"

    print(f"Query: {user_query}\n")

    # Stage 1
    print("Running Stage 1: Intent Extraction...")
    stage1_agent = TransactionStage1SampleAgent(client)
    stage1_events = list(stage1_agent.execute_stage1_events(user_query))

    stage1_output = next(
        (e.get("content") for e in stage1_events if e.get("type") == "intent"),
        None,
    )

    if not stage1_output:
        print("❌ Stage 1 failed")
        return

    print(f"✓ Stage 1 complete")
    print(
        f"  - Metrics found: {len(stage1_output.get('metrics', []))}"
    )

    # Stage 1.5
    print("\nRunning Stage 1.5: Metric Verification...")
    stage1_5_agent = TransactionStage1_5SampleAgent(client)
    stage1_5_events = list(
        stage1_5_agent.execute_stage1_5_events(user_query, stage1_output)
    )

    final_schema = next(
        (e.get("content") for e in stage1_5_events if e.get("type") == "verified_intent"),
        None,
    )

    if not final_schema:
        print("❌ Stage 1.5 failed")
        return

    print(f"✓ Stage 1.5 complete")
    print(f"\nFinal verified metrics:")
    for metric in final_schema.get("metrics", []):
        print(f"  - {metric.get('name')} ({metric.get('type')})")

    return final_schema


def example_3_compare_stage1_vs_stage1_5():
    """Example 3: Compare Stage 1 output vs Stage 1.5 verified output."""
    print("\n" + "=" * 80)
    print("EXAMPLE 3: STAGE 1 vs STAGE 1.5 COMPARISON")
    print("=" * 80 + "\n")

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY")
    if not api_key:
        print("❌ No API key found")
        return

    client = OpenAI(api_key=api_key)

    # Use a query with multiple metrics
    user_query = "Show transactions, total value, average price, and guideline values in Baner"

    print(f"Query: {user_query}\n")
    print("This query mentions 4 metrics:")
    print("  1. Transactions (count)")
    print("  2. Total value (sum)")
    print("  3. Average price (avg)")
    print("  4. Guideline values (sum)\n")

    # Stage 1
    print("Stage 1: Intent Extraction")
    stage1_agent = TransactionStage1SampleAgent(client)
    stage1_events = list(stage1_agent.execute_stage1_events(user_query))
    stage1_output = next(
        (e.get("content") for e in stage1_events if e.get("type") == "intent"), None
    )

    stage1_metrics = stage1_output.get("OUTPUT_JSON_SCHEMA", {}).get("metrics", [])
    print(f"📊 Metrics found by Stage 1: {len(stage1_metrics)}")
    for m in stage1_metrics:
        print(f"  - {m.get('name')}")

    # Stage 1.5
    print("\nStage 1.5: Metric Verification")
    stage1_5_agent = TransactionStage1_5SampleAgent(client)
    stage1_5_events = list(
        stage1_5_agent.execute_stage1_5_events(user_query, stage1_output)
    )
    final_schema = next(
        (e.get("content") for e in stage1_5_events if e.get("type") == "verified_intent"),
        None,
    )

    final_metrics = final_schema.get("metrics", []) if final_schema else []
    print(f"📊 Metrics after Stage 1.5: {len(final_metrics)}")
    for m in final_metrics:
        print(f"  - {m.get('name')} ({m.get('type')})")


def example_4_clarification_handling():
    """Example 4: Handle Stage 1 clarification requests."""
    print("\n" + "=" * 80)
    print("EXAMPLE 4: CLARIFICATION HANDLING")
    print("=" * 80 + "\n")

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY")
    if not api_key:
        print("❌ No API key found")
        return

    client = OpenAI(api_key=api_key)

    # Intentionally ambiguous query
    user_query = "Show me transactions"

    print(f"Query: {user_query}\n")
    print("Note: This query is intentionally vague and will likely trigger clarification\n")

    print("Stage 1: Intent Extraction")
    stage1_agent = TransactionStage1SampleAgent(client)
    stage1_events = list(stage1_agent.execute_stage1_events(user_query))
    stage1_output = next(
        (e.get("content") for e in stage1_events if e.get("type") == "intent"), None
    )

    # Check if clarification was needed
    output_schema = stage1_output.get("OUTPUT_JSON_SCHEMA", {})
    mapped_schema = stage1_output.get("MAPPED_JSON_SCHEMA", {})

    needs_clarification = (
        stage1_output.get("needs_clarification")
        or output_schema.get("needs_clarification")
        or mapped_schema.get("needs_clarification")
    )

    if needs_clarification:
        print("⚠️  Clarification required!")
        clarification = next(
            (e.get("content") for e in stage1_events if e.get("type") == "clarification"),
            None,
        )
        if clarification:
            print(f"\nClarification Question: {clarification.get('message')}")
            print(f"Questions to answer:")
            for q in clarification.get("questions", []):
                print(f"  - {q}")
            print("\nSpace Schema Reference:")
            print(clarification.get("space_schema", ""))
    else:
        print("✓ No clarification needed")
        # Try Stage 1.5
        stage1_5_agent = TransactionStage1_5SampleAgent(client)
        stage1_5_events = list(
            stage1_5_agent.execute_stage1_5_events(user_query, stage1_output)
        )
        final_schema = next(
            (e.get("content") for e in stage1_5_events if e.get("type") == "verified_intent"),
            None,
        )
        if final_schema:
            print(f"\n✓ Verification complete")
            print(f"  - Metrics: {len(final_schema.get('metrics', []))}")


def main():
    """Run all examples."""
    load_env()

    # Check for API key
    if not os.getenv("OPENAI_API_KEY") and not os.getenv("OPENAI_ADMIN_KEY"):
        print("❌ Error: OPENAI_API_KEY or OPENAI_ADMIN_KEY not set")
        print("Please set your OpenAI API key before running examples")
        return

    print("\n" + "=" * 80)
    print("STAGE 1.5 METRIC VERIFICATION - EXAMPLES")
    print("=" * 80)

    try:
        # Run examples
        example_1_complete_workflow()
        example_2_manual_pipeline()
        example_3_compare_stage1_vs_stage1_5()
        example_4_clarification_handling()

        print("\n" + "=" * 80)
        print("ALL EXAMPLES COMPLETED")
        print("=" * 80 + "\n")

    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
