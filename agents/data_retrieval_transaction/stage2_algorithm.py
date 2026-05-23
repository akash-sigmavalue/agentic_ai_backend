from __future__ import annotations

"""
Stage 2: Algorithm Creation for Transaction Agent.

This file builds on Stage 1 output and creates structured algorithms
for metric calculation based on user intent and schema mappings.

Usage:
    python -m agents.data_retrieval_transaction.stage2_algorithm --stage1-output <json_file>

Requirements:
    OPENAI_API_KEY must be available in the environment.
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

from agents.data_retrieval_transaction.stage1_sample import TransactionStage1IntentExtractor
from agents.data_retrieval_transaction.schema import SPACE_SCHEMA, TRANSACTION_QUERY_SCHEMA


logger = logging.getLogger(__name__)


def load_project_env() -> None:
    """Load the nearest project .env file when this sample is run directly."""
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            return


ALGORITHM_CREATION_PROMPT = """
You are an algorithm design agent for real-estate data analytics.

Use the user query, transaction schema, space schema, and Stage 1 intent response
to create a structured calculation algorithm. Do not generate SQL in this stage.

=============================================================
STAGE 2 : ALGORITHM CREATION RULES
=============================================================
1. Give a list of all relevant columns mapped from Transaction schema and Space schema.
2. Filter the relevant columns required to calculate all metrics as per user intent
   from the Transaction schema.
   Create formula if any metric needs to be calculated using multiple columns.
3. Create structured steps using relevant columns & filtered columns to calculate the metric.
4. Print the structured steps in the output JSON.
5. Use only columns present in the supplied schemas. Never invent a column name.
6. Preserve every entity, metric, grouping, filter, and expected output from the
   Stage 1 response.
7. If a metric needs a formula, include the exact formula in plain text using
   schema column names.

INPUT CONTEXT:
  - User Query: Original user question.
  - Transaction Schema: Available transaction table columns and meanings.
  - Space Schema: Mapping of user space levels to schema columns.
  - Stage 1 Intent Response: Intent, metrics, entities, mapped columns, and expected output.

ALGORITHM CREATION STEPS (Follow in order):

Step 1: List All Relevant Columns
  Identify and list ALL columns from TRANSACTION_SCHEMA and SPACE_SCHEMA that are:
  - Required to filter based on the mapped entities (location, property_type, time_period, transaction_category)
  - Required to calculate the requested metrics
  - Needed to preserve the expected output requested by the user
  
Step 2: Determine Required Columns for Metric Calculation
  For each metric in Stage 1 output:
  - Identify which specific TRANSACTION_SCHEMA columns are needed for calculation
  - Identify which SPACE_SCHEMA columns are needed for grouping/filtering
  - List the aggregation function needed (SUM, COUNT, AVG, MAX, MIN, etc.)
  
Step 3: Define Entity Filters
  Create filter conditions for each mapped entity:
  - WHERE {{space_column}} = {{entity_value}}
  - WHERE {{transaction_category}} = {{category_value}}
  - WHERE {{property_type}} = {{property_type_value}}
  - WHERE {{time_period_column}} = {{time_period_value}}
  
Step 4: Create Calculation Steps
  Define structured steps showing:
  - Data Selection: Which columns to select
  - Data Filtering: Apply entity filters
  - Data Grouping: How to group results (if applicable)
  - Aggregation: How to calculate metrics
  - Output: What the result should show
  
Step 5: Generate Structured Algorithm Output
  Return clear, step-by-step algorithm that can be converted to SQL or used by downstream components

=============================================================
TRANSACTION_SCHEMA
=============================================================
{schema}

=============================================================
SPACE_SCHEMA
=============================================================
{space_schema}

=============================================================
STAGE 1 OUTPUT (from intent extraction)
=============================================================
{stage1_output}

=============================================================
USER QUERY
=============================================================
{user_query}

=============================================================
RESPONSE FORMAT (strict JSON - no markdown, no preamble)
=============================================================
{{
  "stage": "2",
  "algorithm_type": "",
  "user_query_context": "",
  "stage1_intent_summary": {{
    "analysis_type": "",
    "intent": "",
    "metrics": [],
    "entities": {{}},
    "expected_output": ""
  }},
  "relevant_columns": {{
    "transaction_schema_columns": [
      {{
        "column": "",
        "source": "transaction_schema",
        "why_relevant": "",
        "mapped_from_stage1": true
      }}
    ],
    "space_schema_columns": [
      {{
        "space_level": "",
        "column": "",
        "source": "space_schema",
        "why_relevant": "",
        "mapped_from_stage1": true
      }}
    ]
  }},
  "filtered_metric_columns": [
    {{
      "metric_name": "",
      "required_transaction_columns": [],
      "required_space_columns": [],
      "aggregation_function": "",
      "formula": "",
      "description": ""
    }}
  ],
  "entity_filters": [
    {{
      "filter_type": "",
      "column": "",
      "value": "",
      "operation": "WHERE",
      "source_schema": ""
    }}
  ],
  "algorithm_steps": [
    {{
      "step_number": 1,
      "step_name": "",
      "description": "",
      "columns_involved": [],
      "operation": ""
    }}
  ],
  "structured_steps_output": "",
  "expected_output_format": "",
  "data_constraints": []
}}
"""


SYSTEM_PROMPT = """
You are the Stage 2 algorithm creator for a real-estate transaction analytics pipeline.
Your job is to convert Stage 1 intent output into a precise metric-calculation
algorithm. Use the user query, transaction schema, space schema, and Stage 1
response as context. Return only valid JSON matching the requested structure.
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


def extract_transaction_columns(schema: str) -> set[str]:
    """Extract transaction column names from schema text."""
    return {
        match.group(1)
        for match in re.finditer(r"^\s{2}([a-zA-Z_][a-zA-Z0-9_]*)\s*:", schema, re.MULTILINE)
    }


def extract_space_columns(schema: str) -> set[str]:
    """Extract schema column names from the pipe-delimited space schema."""
    columns: set[str] = set()
    for line in schema.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) >= 3 and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", parts[2]):
            columns.add(parts[2])
    return columns


TRANSACTION_COLUMNS = extract_transaction_columns(TRANSACTION_QUERY_SCHEMA)
SPACE_COLUMNS = extract_space_columns(SPACE_SCHEMA)
ALLOWED_SCHEMA_COLUMNS = TRANSACTION_COLUMNS | SPACE_COLUMNS


class TransactionStage2AlgorithmCreator:
    """
    Creates detailed algorithms for metric calculation based on Stage 1 intent output.
    """

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self.client = client
        self.model = model
        self.last_usage: Any = None

    def create_algorithm(
        self,
        user_query: str,
        stage1_output: dict,
        history: list[dict] | None = None,
    ) -> dict:
        """Create algorithm based on Stage 1 output."""
        stage1_output_str = json.dumps(stage1_output, indent=2)
        
        prompt = ALGORITHM_CREATION_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            space_schema=SPACE_SCHEMA,
            user_query=user_query,
            stage1_output=stage1_output_str,
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
        
        print("\n" + "="*80)
        print("STAGE 2 ALGORITHM CREATION PROMPT:")
        print("="*80)
        print(prompt)
        print("="*80 + "\n")
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            timeout=30,
        )
        self.last_usage = response.usage
        raw = response.choices[0].message.content.strip()
        
        print("\n" + "="*80)
        print("RAW LLM RESPONSE:")
        print("="*80)
        print(raw)
        print("="*80 + "\n")
        
        algorithm = parse_json(raw, default=None)
        
        if algorithm is None:
            raise ValueError(
                f"Stage2AlgorithmCreator failed to parse LLM response: {raw[:300]}"
            )
        
        return algorithm


class TransactionStage2SampleAgent:
    """Stage 2 wrapper with event-style output."""

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self.domain_key = "transaction"
        self.display_name = "Transaction Agent"
        self.algorithm_creator = TransactionStage2AlgorithmCreator(client, model=model)

    def _event(self, event_type: str, content: Any, **kwargs: Any) -> dict:
        payload = {"type": event_type, "content": content}
        payload.update(kwargs)
        return payload

    def execute_stage2_events(
        self,
        user_query: str,
        stage1_output: dict,
        history: list[dict] | None = None,
    ) -> Iterable[dict]:
        try:
            yield self._event("stage", f"{self.display_name} - Stage 2: Creating algorithm...")
            algorithm = self.algorithm_creator.create_algorithm(
                user_query, stage1_output, history=history
            )

            if self.algorithm_creator.last_usage is not None:
                yield self._event(
                    "token_usage_raw",
                    None,
                    stage_name=f"{self.domain_key}.s2_algorithm",
                    usage=self.algorithm_creator.last_usage,
                )

            # Print algorithm output
            print("\n" + "="*80)
            print("ALGORITHM OUTPUT:")
            print("="*80)
            print(json.dumps(algorithm, indent=2))
            print("="*80 + "\n")

            yield self._event("algorithm", algorithm, agent=self.domain_key)
            
            yield self._event(
                "debug_trace",
                {
                    "phase": "plan",
                    "step": "algorithm_creation",
                    "summary": "Algorithm created by Stage 2 Algorithm Creator",
                    "algorithm_type": algorithm.get("algorithm_type"),
                    "metrics": [
                        metric.get("metric_name")
                        for metric in algorithm.get("filtered_metric_columns", [])
                        if isinstance(metric, dict) and metric.get("metric_name")
                    ],
                    "steps_count": len(algorithm.get("algorithm_steps", [])),
                },
                agent=self.domain_key,
            )

        except Exception as error:
            yield self._event("error", f"{self.display_name} Stage 2 failed: {str(error)}")


def run_stage2(
    user_query: str,
    stage1_output: dict,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
) -> list[dict]:
    """Convenience function for scripts/tests."""
    load_project_env()
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY")
    if not resolved_api_key:
        raise RuntimeError(
            "Missing OpenAI credentials. Add OPENAI_API_KEY to the project .env file, "
            "set it in your shell, or pass --api-key when running this sample."
        )
    client = OpenAI(api_key=resolved_api_key)
    agent = TransactionStage2SampleAgent(client=client, model=model)
    return list(agent.execute_stage2_events(user_query, stage1_output))


def needs_clarification(stage1_output: dict) -> bool:
    """Return True when Stage 1 says more user input is required."""
    output_schema = stage1_output.get("OUTPUT_JSON_SCHEMA") or {}
    mapped_schema = stage1_output.get("MAPPED_JSON_SCHEMA") or {}
    return bool(
        stage1_output.get("needs_clarification")
        or output_schema.get("needs_clarification")
        or mapped_schema.get("needs_clarification")
    )


def stage1_clarification_payload(stage1_output: dict) -> dict[str, Any]:
    """Convert new Stage 1 clarification output into pipeline clarification payload."""
    return {
        "message": get_clarification_question(stage1_output),
        "questions": [get_clarification_question(stage1_output)],
        "missing_fields": stage1_output.get("missing_fields") or [],
        "field_definitions": stage1_output.get("field_definitions") or {},
        "stage1_output": stage1_output,
    }


def get_clarification_question(stage1_output: dict) -> str:
    """Extract the best clarification question from a Stage 1 response."""
    output_schema = stage1_output.get("OUTPUT_JSON_SCHEMA") or {}
    mapped_schema = stage1_output.get("MAPPED_JSON_SCHEMA") or {}
    questions = stage1_output.get("clarification_questions") or []
    if questions:
        return "\n".join(str(question) for question in questions)
    return (
        stage1_output.get("clarification_question")
        or output_schema.get("clarification_question")
        or mapped_schema.get("clarification_question")
        or "Please provide the missing required details."
    )


def _as_list(value: Any) -> list[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _clean_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("name") or value.get("label")
    return str(value or "").strip()


def _append_unique(items: list[Any], item: Any) -> None:
    text = json.dumps(item, sort_keys=True, default=str) if isinstance(item, dict) else str(item)
    existing = {
        json.dumps(existing_item, sort_keys=True, default=str)
        if isinstance(existing_item, dict)
        else str(existing_item)
        for existing_item in items
    }
    if text and text not in existing:
        items.append(item)


def _metric_from_name(metric_name: str, algorithm_metrics: list[dict]) -> dict:
    metric_lower = metric_name.lower()
    matching_algorithm_metric = next(
        (
            metric
            for metric in algorithm_metrics
            if isinstance(metric, dict)
            and str(metric.get("metric_name", "")).lower() == metric_lower
        ),
        {},
    )
    formula = str(matching_algorithm_metric.get("formula") or "").strip()

    if "rate" in metric_lower or "price per" in metric_lower or "per sq" in metric_lower:
        derived_expression = (
            "ROUND(SUM(agreement_price)::numeric / "
            "NULLIF(SUM(net_carpet_area_sq_m), 0) / 10.764, 2)"
        )
        alias = metric_name or "rate_per_sq_ft"
    elif "count" in metric_lower or "transaction" in metric_lower or "unit" in metric_lower:
        derived_expression = "COUNT(*)"
        alias = metric_name or "transaction_count"
    elif "guideline" in metric_lower:
        derived_expression = "SUM(guideline_value)"
        alias = metric_name or "total_guideline_value"
    else:
        derived_expression = "SUM(agreement_price)"
        alias = metric_name or "total_agreement_price"

    return {
        "name": metric_name,
        "alias": alias,
        "derived_expression": formula or derived_expression,
    }


def _normalize_metrics(output_schema: dict, algorithm: dict) -> list[dict]:
    algorithm_metrics = [
        metric
        for metric in algorithm.get("filtered_metric_columns", []) or []
        if isinstance(metric, dict)
    ]
    raw_metrics = output_schema.get("metrics")
    metrics: list[dict] = []

    if isinstance(raw_metrics, list):
        for item in raw_metrics:
            if isinstance(item, dict):
                name = str(item.get("alias") or item.get("name") or item.get("metric_name") or "").strip()
                metric = dict(item)
                if name and not metric.get("derived_expression"):
                    metric.update(_metric_from_name(name, algorithm_metrics))
                _append_unique(metrics, metric)
            else:
                name = _clean_text(item)
                if name:
                    _append_unique(metrics, _metric_from_name(name, algorithm_metrics))
    elif isinstance(raw_metrics, dict):
        for name, value in raw_metrics.items():
            metric_name = str(name or value).strip()
            if metric_name:
                _append_unique(metrics, _metric_from_name(metric_name, algorithm_metrics))
    else:
        name = _clean_text(raw_metrics)
        if name:
            for part in re.split(r",|\band\b", name):
                part = part.strip()
                if part:
                    _append_unique(metrics, _metric_from_name(part, algorithm_metrics))

    for algorithm_metric in algorithm_metrics:
        name = str(algorithm_metric.get("metric_name") or "").strip()
        if name:
            _append_unique(metrics, _metric_from_name(name, algorithm_metrics))

    return metrics


def _collect_entity_values(entities: Any) -> dict[str, Any]:
    if isinstance(entities, dict):
        return entities
    return {}


def _add_space_entity(react_entities: dict, column: str, value: Any) -> None:
    text = _clean_text(value)
    if not column or not text:
        return

    react_entities.setdefault("space_filters", {})
    react_entities["space_filters"][column] = text

    if column == "project_name":
        _append_unique(react_entities.setdefault("projects", []), {"value": text, "semantic_level": "project"})
    elif column == "city_name":
        _append_unique(react_entities.setdefault("locations", []), {"value": text, "semantic_level": "city"})
    elif column in {"location_name", "micro_market", "sub_locality", "village_name"}:
        _append_unique(react_entities.setdefault("locations", []), {"value": text, "semantic_level": "locality"})


def _normalize_entities(output_schema: dict, mapped_schema: dict, algorithm: dict) -> dict:
    raw_entities = _collect_entity_values(output_schema.get("entities"))
    mapped_entities = _collect_entity_values(mapped_schema.get("entities"))
    react_entities: dict[str, Any] = {
        "locations": [],
        "projects": [],
        "property_types": [],
        "space_filters": {},
        "category_filters": {},
    }

    for key, value in raw_entities.items():
        key_text = str(key).strip()
        if key_text in SPACE_COLUMNS or key_text in {"sub_locality", "village_name"}:
            _add_space_entity(react_entities, key_text, value)
        elif key_text in {"space", "space_value", "location", "locality", "micromarket", "city", "project"}:
            mapped_space_column = (
                mapped_entities.get("space_field")
                or mapped_entities.get("space_column")
                or mapped_entities.get("location")
                or mapped_entities.get("location_name")
            )
            if mapped_space_column:
                _add_space_entity(react_entities, str(mapped_space_column), value)
        elif key_text == "property_type":
            for item in _as_list(value):
                text = _clean_text(item)
                if text:
                    _append_unique(react_entities["property_types"], {"value": text})
                    react_entities["category_filters"]["property_type"] = text
        elif key_text in {"transaction_category", "unit_configuration", "project_type", "sale_type"}:
            text = _clean_text(value)
            if text:
                react_entities["category_filters"][key_text] = text

    for entity_filter in algorithm.get("entity_filters", []) or []:
        if not isinstance(entity_filter, dict):
            continue
        column = str(entity_filter.get("column") or "").strip()
        value = entity_filter.get("value")
        if column in SPACE_COLUMNS or column in {"sub_locality", "village_name"}:
            _add_space_entity(react_entities, column, value)
        elif column in {"transaction_category", "property_type", "unit_configuration", "project_type", "sale_type"}:
            text = _clean_text(value)
            if text:
                react_entities["category_filters"][column] = text
                if column == "property_type":
                    _append_unique(react_entities["property_types"], {"value": text})

    return react_entities


def _normalize_time_filters(output_schema: dict, algorithm: dict) -> dict:
    filters: dict[str, Any] = {}
    entities = _collect_entity_values(output_schema.get("entities"))
    for key in ("year", "quarter", "transaction_date", "time_period"):
        value = entities.get(key)
        if value not in (None, "", [], {}):
            if key == "time_period":
                filters["time_period"] = value
            else:
                filters[key] = value

    for entity_filter in algorithm.get("entity_filters", []) or []:
        if not isinstance(entity_filter, dict):
            continue
        column = str(entity_filter.get("column") or "").strip()
        if column in {"year", "quarter", "transaction_date"}:
            filters[column] = entity_filter.get("value")

    return filters


def normalize_stage_outputs_for_react(
    user_query: str,
    stage1_output: dict,
    stage2_algorithm: dict,
) -> dict:
    """
    Preserve the new Stage 1/Stage 2 output while adding the flat ReAct fields
    needed by the existing SQL loop.
    """
    output_schema = stage1_output.get("OUTPUT_JSON_SCHEMA") or stage1_output
    mapped_schema = stage1_output.get("MAPPED_JSON_SCHEMA") or {}
    analysis_type = (
        output_schema.get("analysis_type")
        or mapped_schema.get("analysis_type")
        or stage2_algorithm.get("stage1_intent_summary", {}).get("analysis_type")
        or "summary"
    )

    react_intent = {
        "analysis_type": analysis_type,
        "intent": output_schema.get("intent") or mapped_schema.get("intent") or user_query,
        "metrics": _normalize_metrics(output_schema, stage2_algorithm),
        "entities": _normalize_entities(output_schema, mapped_schema, stage2_algorithm),
        "filters": _normalize_time_filters(output_schema, stage2_algorithm),
        "group_by": [],
        "order_by": [],
        "time_series": str(analysis_type).lower() == "trend",
        "expected_output": output_schema.get("expected output") or output_schema.get("expected_output") or "",
        "user_query": user_query,
        "stage1_output": stage1_output,
        "stage2_algorithm": stage2_algorithm,
        "route": "internal_db",
        "needs_clarification": False,
    }

    if react_intent["time_series"]:
        react_intent["group_by"] = ["year", "quarter"]

    return react_intent


def collect_clarification_answers(stage1_output: dict) -> dict[str, str]:
    """Ask clarification questions in the terminal and return user answers."""
    missing_fields = stage1_output.get("missing_fields") or []
    answers: dict[str, str] = {}

    print("\n" + "=" * 80)
    print("STAGE 1 CLARIFICATION REQUIRED")
    print("=" * 80)
    print(get_clarification_question(stage1_output))

    if missing_fields:
        print("\nMissing fields:")
        for field in missing_fields:
            print(f"- {field}")
        print()
        for field in missing_fields:
            answers[str(field)] = input(f"Enter {field}: ").strip()
    else:
        answers["clarification"] = input("Enter clarification answer: ").strip()

    print("=" * 80 + "\n")
    return answers


def append_clarification_to_query(user_query: str, answers: dict[str, str]) -> str:
    """Create a fuller user query after terminal clarification."""
    answer_lines = [
        f"{field}: {value}"
        for field, value in answers.items()
        if value
    ]
    if not answer_lines:
        return user_query
    return (
        f"{user_query}\n\n"
        "Additional clarification provided by user:\n"
        + "\n".join(answer_lines)
    )


def run_stage1_then_stage2_interactive(
    user_query: str,
    stage1_model: str = "gpt-4o-mini",
    stage2_model: str = "gpt-4o-mini",
    api_key: str | None = None,
    max_clarifications: int = 3,
) -> dict[str, Any]:
    """
    Terminal workflow:
    1. Run Stage 1 from the user query.
    2. Ask clarification questions in terminal when Stage 1 needs them.
    3. Run Stage 2 algorithm generation from the final Stage 1 output.
    """
    load_project_env()
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY")
    if not resolved_api_key:
        raise RuntimeError(
            "Missing OpenAI credentials. Add OPENAI_API_KEY to the project .env file, "
            "set it in your shell, or pass --api-key."
        )

    client = OpenAI(api_key=resolved_api_key)
    stage1 = TransactionStage1IntentExtractor(client=client, model=stage1_model)
    stage2 = TransactionStage2AlgorithmCreator(client=client, model=stage2_model)

    current_query = user_query
    stage1_output: dict[str, Any] | None = None

    for attempt in range(max_clarifications + 1):
        print("\n" + "=" * 80)
        print(f"RUNNING STAGE 1 INTENT EXTRACTION - ATTEMPT {attempt + 1}")
        print("=" * 80)
        print(current_query)
        print("=" * 80 + "\n")

        stage1_output = stage1.extract(current_query)
        if not needs_clarification(stage1_output):
            break

        if attempt >= max_clarifications:
            raise RuntimeError("Stage 1 still needs clarification after the maximum attempts.")

        answers = collect_clarification_answers(stage1_output)
        current_query = append_clarification_to_query(current_query, answers)

    if stage1_output is None:
        raise RuntimeError("Stage 1 did not produce output.")

    print("\n" + "=" * 80)
    print("RUNNING STAGE 2 ALGORITHM GENERATION")
    print("=" * 80 + "\n")
    algorithm = stage2.create_algorithm(current_query, stage1_output)

    result = {
        "user_query": current_query,
        "stage1_output": stage1_output,
        "stage2_algorithm": algorithm,
        "usage": {
            "stage1": stage1.last_usage,
            "stage2": stage2.last_usage,
        },
    }

    print("\n" + "=" * 80)
    print("FINAL STAGE 1 + STAGE 2 OUTPUT")
    print("=" * 80)
    print(json.dumps(result, indent=2, default=str))
    print("=" * 80 + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run standalone Transaction Stage 2. If --stage1-output is omitted, "
            "the script runs Stage 1 first, asks clarification questions, then runs Stage 2."
        )
    )
    parser.add_argument("user_query", nargs="?", help="Original user query")
    parser.add_argument(
        "--stage1-output",
        default=None,
        help="Path to Stage 1 output JSON file or JSON string",
    )
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI model to use for Stage 2")
    parser.add_argument("--stage1-model", default="gpt-4o-mini", help="OpenAI model to use for Stage 1")
    parser.add_argument("--max-clarifications", type=int, default=3, help="Maximum terminal clarification rounds")
    parser.add_argument("--api-key", default=None, help="Optional OpenAI API key")
    args = parser.parse_args()

    user_query = args.user_query or input("Enter user query: ").strip()
    if not user_query:
        raise SystemExit("User query is required.")

    if args.stage1_output:
        try:
            if os.path.isfile(args.stage1_output):
                with open(args.stage1_output, "r") as f:
                    stage1_output = json.load(f)
            else:
                stage1_output = json.loads(args.stage1_output)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error loading Stage 1 output: {e}")
            return

        for event in run_stage2(user_query, stage1_output, model=args.model, api_key=args.api_key):
            print(json.dumps(event, indent=2, default=str))
        return

    run_stage1_then_stage2_interactive(
        user_query=user_query,
        stage1_model=args.stage1_model,
        stage2_model=args.model,
        api_key=args.api_key,
        max_clarifications=args.max_clarifications,
    )


if __name__ == "__main__":
    main()
