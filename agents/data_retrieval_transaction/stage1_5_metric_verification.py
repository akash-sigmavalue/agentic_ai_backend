from __future__ import annotations

"""
Stage 1.5 - Metric Verification and Completion.

This file validates Stage 1 output to ensure all metrics requested in the user
query are present. It combines OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA into
a single FINAL_JSON_SCHEMA and adds any missing metrics.

Usage:
    python -m agents.data_retrieval_transaction.stage1_5_metric_verification <stage1_output_json>

Requirements:
    OPENAI_API_KEY must be available in the environment.
    Stage 1 output JSON file or dict.
"""

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from openai import OpenAI


logger = logging.getLogger(__name__)


def load_project_env() -> None:
    """Load the nearest project .env file when this sample is run directly."""
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            return


METRIC_VERIFICATION_PROMPT = """
You are a metric verification and completion agent for real-estate data analytics.

Your task is to:
1. Identify ALL metrics explicitly requested in the original user query
2. Compare them against the metrics list from Stage 1 output
3. Identify any missing metrics that should be included
4. Return a complete and verified metrics list

=============================================================
STAGE 1.5 - CHECK & FIX METRIC COMPLETENESS
=============================================================

CRITICAL RULES:

Rule 1: Extract all metric requests from the user query
  - Look for explicit metric names or calculations mentioned
  - Examples: "total sales", "average price", "transaction count", "price per sqm"
  - Extract both explicit and implied metrics
  - Store with metric name and description from query

Rule 2: Compare user metrics against Stage 1 metrics list
  - For each extracted metric from the user query, check if it exists in Stage 1 metrics
  - Match by semantic similarity (e.g., "total transactions" = "transaction_count")
  - Identify MISSING metrics NOT in Stage 1 output

Rule 3: Fix missing metrics
  - For each missing metric, add it to the metrics list with proper structure:
    {{
      "name": "metric_name",
      "alias": "metric_alias",
      "type": "aggregation_type",
      "description": "what this metric represents"
    }}

Rule 4: Return complete FINAL_JSON_SCHEMA
  - Combine OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA from Stage 1
  - Use mapped values where available
  - Override with enriched metrics list (original + fixed metrics)
  - Preserve all other fields from Stage 1
  - Mark verification_complete = true
  - Include summary of changes made

=============================================================
INPUT CONTEXT
=============================================================

Original User Query:
{user_query}

Stage 1 Output (OUTPUT_JSON_SCHEMA):
{output_schema}

Stage 1 Output (MAPPED_JSON_SCHEMA):
{mapped_schema}

=============================================================
VERIFICATION STEPS
=============================================================

Step 1: Extract all metrics from original user query
  - List each metric mentioned or implied
  
Step 2: Extract metrics from Stage 1 output
  - List all metrics currently in Stage 1 metrics field
  
Step 3: Cross-check and identify missing metrics
  - For each user metric, check if present in Stage 1
  - Document missing metrics with their descriptions
  
Step 4: Create complete metrics list
  - Include all Stage 1 metrics
  - Add all missing metrics identified in Step 3
  - Ensure each metric has name, alias, type, description

Step 5: Generate FINAL_JSON_SCHEMA
  - Combine both Stage 1 schemas
  - Replace metrics field with complete list
  - Add verification metadata

=============================================================
RESPONSE FORMAT (strict JSON - no markdown, no preamble)
Return ONLY FINAL_JSON_SCHEMA with no other fields.
=============================================================
{{
  "analysis_type": "",
  "intent": "",
  "metrics": [
    {{
      "name": "",
      "alias": "",
      "type": "",
      "description": ""
    }}
  ],
  "entities": {{
    "space_field": "location_name",
    "property_type": "property_type",
    "time_period": "year",
    "transaction_category": "transaction_category"
  }},
  "expected_output": "",
  "verification_complete": true,
  "needs_clarification": false,
  "clarification_question": ""
}}
"""

SYSTEM_PROMPT = """
You are the Stage 1.5 metric verification agent for a real-estate transaction analytics pipeline.
Your job is to:
1. Identify all metrics requested in the original user query
2. Compare against Stage 1 metrics list
3. Add any missing metrics
4. Return a complete FINAL_JSON_SCHEMA combining both Stage 1 schemas

Be thorough in identifying metrics. Common metrics include:
- Count metrics (transaction count, unit count)
- Sum metrics (total sales value, total agreement price, total guideline value)
- Average metrics (average price, average price per sqm)
- Rate metrics (sales rate, absorption rate)
- Trend metrics (month-on-month, quarter-on-quarter)

Return ONLY valid JSON with no markdown or preamble.
"""


def parse_json(text: str, default: Any) -> Any:
    """Parse JSON from LLM response."""
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    logger.warning("parse_json: could not parse response. Preview: %s", text[:200])
    return default


class TransactionStage1_5MetricVerifier:
    """
    Verifies and completes metrics from Stage 1 output.
    Combines OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA into FINAL_JSON_SCHEMA.
    """

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self.client = client
        self.model = model
        self.last_usage: Any = None

    def verify_metrics(
        self,
        user_query: str,
        stage1_output: dict,
        history: list[dict] | None = None,
    ) -> dict:
        """Verify and complete metrics from Stage 1 output."""
        # Extract schemas from Stage 1 output
        output_schema = stage1_output.get("OUTPUT_JSON_SCHEMA") or stage1_output
        mapped_schema = stage1_output.get("MAPPED_JSON_SCHEMA") or {}

        output_schema_str = json.dumps(output_schema, indent=2)
        mapped_schema_str = json.dumps(mapped_schema, indent=2)

        prompt = METRIC_VERIFICATION_PROMPT.format(
            user_query=user_query,
            output_schema=output_schema_str,
            mapped_schema=mapped_schema_str,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.strip(),
            },
        ]

        if history:
            for message in history[-4:]:
                messages.append(
                    {
                        "role": message["role"],
                        "content": message["content"],
                    }
                )

        messages.append({"role": "user", "content": prompt})

        print("\n" + "=" * 80)
        print("STAGE 1.5 METRIC VERIFICATION PROMPT:")
        print("=" * 80)
        print(prompt)
        print("=" * 80 + "\n")

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            timeout=20,
        )
        self.last_usage = response.usage
        raw = response.choices[0].message.content.strip()

        print("\n" + "=" * 80)
        print("RAW LLM RESPONSE:")
        print("=" * 80)
        print(raw)
        print("=" * 80 + "\n")

        final_json_schema = parse_json(raw, default=None)

        if final_json_schema is None:
            raise ValueError(
                f"TransactionStage1_5MetricVerifier failed to parse LLM response: {raw[:300]}"
            )

        # Print final schema
        print("\n" + "=" * 80)
        print("FINAL_JSON_SCHEMA:")
        print("=" * 80)
        print(json.dumps(final_json_schema, indent=2))
        print("=" * 80 + "\n")

        logger.info(
            "Stage 1.5 verification complete: verification_complete=%s, metrics_count=%s",
            final_json_schema.get("verification_complete"),
            len(final_json_schema.get("metrics", [])),
        )

        return final_json_schema


class TransactionStage1_5SampleAgent:
    """Stage 1.5 wrapper with event-style output."""

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self.domain_key = "transaction"
        self.display_name = "Transaction Agent"
        self.metric_verifier = TransactionStage1_5MetricVerifier(client, model=model)

    def _event(
        self, event_type: str, content: Any, **kwargs: Any
    ) -> dict:
        payload = {"type": event_type, "content": content}
        payload.update(kwargs)
        return payload

    def execute_stage1_5_events(
        self,
        user_query: str,
        stage1_output: dict,
        history: list[dict] | None = None,
    ) -> Iterable[dict]:
        try:
            yield self._event(
                "stage",
                f"{self.display_name} - Stage 1.5: Verifying and completing metrics...",
            )

            final_schema = self.metric_verifier.verify_metrics(
                user_query, stage1_output, history=history
            )

            if self.metric_verifier.last_usage is not None:
                yield self._event(
                    "token_usage_raw",
                    None,
                    stage_name=f"{self.domain_key}.s1_5_verification",
                    usage=self.metric_verifier.last_usage,
                )

            yield self._event(
                "verified_intent",
                final_schema,
                agent=self.domain_key,
                stage="1.5",
            )

            yield self._event(
                "debug_trace",
                {
                    "phase": "verify",
                    "step": "metric_verification",
                    "summary": "Metrics verified and completed by Stage 1.5",
                    "verification_complete": final_schema.get("verification_complete"),
                    "total_metrics": len(final_schema.get("metrics", [])),
                    "analysis_type": final_schema.get("analysis_type"),
                },
                agent=self.domain_key,
            )

        except Exception as error:
            yield self._event(
                "error",
                f"{self.display_name} Stage 1.5 failed: {str(error)}",
            )


def run_stage1_5(
    user_query: str,
    stage1_output: dict,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
) -> list[dict]:
    """Convenience function for scripts/tests."""
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
    agent = TransactionStage1_5SampleAgent(client=client, model=model)
    return list(agent.execute_stage1_5_events(user_query, stage1_output))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run standalone Transaction Agent Stage 1.5 metric verification."
    )
    parser.add_argument("user_query", help="Original user query")
    parser.add_argument(
        "--stage1-output",
        required=True,
        help="Path to Stage 1 output JSON file or JSON string",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAI model for metric verification",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Optional OpenAI API key. Defaults to OPENAI_API_KEY from .env.",
    )
    args = parser.parse_args()

    # Load stage1_output
    stage1_output_str = args.stage1_output
    try:
        if stage1_output_str.startswith("{"):
            # Assume it's a JSON string
            stage1_output = json.loads(stage1_output_str)
        else:
            # Assume it's a file path
            with open(stage1_output_str, "r") as f:
                stage1_output = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Error loading stage1_output: {e}")
        return

    for event in run_stage1_5(
        args.user_query,
        stage1_output,
        model=args.model,
        api_key=args.api_key,
    ):
        print(json.dumps(event, indent=2, default=str))


if __name__ == "__main__":
    main()
