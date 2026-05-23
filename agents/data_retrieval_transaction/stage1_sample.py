from __future__ import annotations

"""
Standalone sample for Transaction Agent Stage 1.

This file is intentionally separate from the live transaction agent files.
It contains only the code needed to run Stage 1: intent/entity extraction.

Usage:
    python -m agents.data_retrieval_transaction.stage1_sample "Show transactions in Baner"

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


logger = logging.getLogger(__name__)


def load_project_env() -> None:
    """Load the nearest project .env file when this sample is run directly."""
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            return



TRANSACTION_QUERY_SCHEMA = """
Use only the following tables, columns, and meanings.

transactions:
  project_id: Unique project identifier
  internal_index_id: Internal system project ID
  project_name: Name of project
  village_name_marathi: Village name in Marathi
  location_id: Location identifier
  location_name: Location of transaction
  village_name: Village name in English
  year: Transaction year
  quarter: Transaction quarter
  city_id: City identifier
  city_name: City of transaction
  transaction_category_id: Transaction category ID
  sub_registrar_office_code: SRO office code
  sub_registrar_office_name: SRO office name
  document_number: Registration document number
  transaction_type: Type of transaction
  agreement_price: Deal/agreement value
  guideline_value: Government determined value
  property_description: Property details text
  transaction_date: Date of transaction
  floor_number: Floor of unit
  unit_number: Identifier for a specific apartment, flat, or suite within a building
  property_type_raw: Raw property type text
  net_carpet_area_sq_m: Net carpet area in square meters
  balcony_sq_m: Balcony area in square meters
  terrace_sq_m: Terrace area in square meters
  seller_name: Seller name
  buyer_name: Buyer name
  transaction_category: Category of transaction such as sale, lease, others
  internal_document_number: Internal doc reference
  micr_number: Bank MICR number
  bank_type: Bank type or category
  party_code: Party classification code
  date_of_agreement_execution: Agreement execution date
  stamp_duty_paid: Stamp duty amount
  registration_fee: Registration fee paid
  project_latitude: Project latitude
  project_longitude: Project longitude
  location_latitude: Location latitude
  location_longitude: Location longitude
  property_type: Standardized property type
  unit_configuration: BHK configuration
  buyer_pincode: Buyer postal code
  buyer_locality: Buyer locality
  buyer_district: Buyer district
  buyer_state: Buyer state
  is_llm_processed: Processed by AI flag
  is_manual_processed: Processed manually flag
  tower_name: Building or tower name
  is_duplicate: Duplicate record flag
  sale_type: Primary or secondary sale
  project_type: Residential or commercial type
  country_name: Country name
  state_name: State name
  micro_market: Micro market area
  sub_locality: Sub-local area
  pincode: Property postal code
  parking_count: Number of parking slots
  facing_direction: Property facing direction
  view_type: View from property
  furnishing_status: Furnishing level
  condition_status: Property condition
  source_accessibility: Data access status
  source_accessibility_way: Access method such as api or download
  sourcing_cost: Processing or source cost
  sourcing_time: Processing time
  data_type: Registered document
  data_source: Source of data such as IGR or DLD

Semantic category columns:
  The following columns often contain repeated categorical values and should be
  semantically resolved against distinct database values before SQL generation:
  transaction_category, property_type, unit_configuration, project_type,
  sale_type, furnishing_status, condition_status, facing_direction, view_type,
  bank_type.
"""


SPACE_SCHEMA = """
1 | unit | unit_number
2 | building | tower_name
3 | parcel/survey/CTS/khasra/plot no | plot_number
4 | project | project_name
5 | location | location_name
6 | micromarket | micro_market
7 | city | city_name
8 | state | state_name
9 | country | country_name
"""


INTENT_EXTRACT_PROMPT = """
You are an intent extraction agent for a real-estate intelligence platform.

Convert the user's natural language query into a structured JSON intent object.
This intent drives SQL generation - it must capture EVERYTHING the user asked for.

=============================================================
STAGE 1 : EXTRACTION RULES (VALIDATION-FIRST APPROACH)
=============================================================

CRITICAL: VALIDATE BEFORE EXTRACTING - DO NOT SKIP THIS STEP

Compulsory Fields (ALL MUST BE EXPLICITLY PRESENT - NO INFERENCE):
  1. SPACE_SCHEMA field: Must explicitly select ONE space level (location_name, micro_market, city_name, state_name, country_name, or other from SPACE_SCHEMA)
     - REQUIRED VALUE: Exact location/city/state/market name from user query
     - NOT ALLOWED: Assuming a default area, inferring closest location, or using current/latest default
  
  2. time_period: Must explicitly specify year, quarter, or date range
     - REQUIRED VALUE: Specific year/quarter/month/date range (e.g., "2024 Q1", "January 2024", "2024")
     - NOT ALLOWED: Inferring current period, latest quarter, or assuming any time range
  
  3. transaction_category: Must explicitly specify the transaction type
     - REQUIRED VALUE: One of "sale", "lease", "ownership_transfer", "mortgage" (from transaction_category column)
     - NOT ALLOWED: Inferring "sale" from "sales value", assuming category, using all categories
  
  4. property_type: Must explicitly specify property classification
     - REQUIRED VALUE: One of "residential", "commercial", "industrial", "mixed-use"
     - NOT ALLOWED: Assuming all properties, inferring type from context

VALIDATION SEQUENCE (STRICT - DO NOT SKIP):
  Step 1: Check if user query contains EXPLICIT values for ALL 4 compulsory fields above
  Step 2A: IF ANY FIELD IS MISSING -> IMMEDIATELY return clarification response (set needs_clarification=true, skip extraction)
  Step 2B: IF ALL FIELDS ARE PRESENT -> Proceed to extraction rules below

EXTRACTION RULES (Only if Step 2B validation passes):
  
  Rule 1: Extract entity, metric, intent, expected output from user query
          Fill these in OUTPUT_JSON_SCHEMA format
  
  Rule 2: For space level entity extraction, follow the fixed mapping provided in SPACE_SCHEMA
          Match user values exactly to SPACE_SCHEMA column names
  
  Rule 3: If the same entity value exists in multiple space columns, do NOT assume
          Return ambiguity and ask user to clarify which column they meant (set needs_clarification=true)
  
  Rule 4: Map values from OUTPUT_JSON_SCHEMA using TRANSACTION_SCHEMA & SPACE_SCHEMA column definitions
          Create MAPPED_JSON_SCHEMA mapping ALL compulsory entities to actual column names:
          - SPACE_SCHEMA field entity -> mapped column (location_name, micro_market, city_name, etc.)
          - property_type entity -> "property_type" (from TRANSACTION_SCHEMA)
          - time_period entity -> "year" or "quarter" (from TRANSACTION_SCHEMA)
          - transaction_category entity -> "transaction_category" (from TRANSACTION_SCHEMA)
  
  Rule 5: Return both OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA in response with needs_clarification=false

=============================================================
SPACE_SCHEMA  (for understanding available dimensions)
=============================================================
{space_schema}

=============================================================
TRANSACTION_SCHEMA  (for understanding available dimensions)
=============================================================
{schema}

=============================================================
USER QUERY
=============================================================
{user_query}

=============================================================
RESPONSE FORMAT (strict JSON - no markdown, no preamble)
IF needs_clarification=true: Return clarification response with missing field details
IF needs_clarification=false: Return both OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA

Clarification Response Format (when validation fails):
{{
  "needs_clarification": true,
  "clarification_question": "Please provide: [list the exact missing compulsory fields]",
  "missing_fields": ["field1", "field2", ...],
  "field_definitions": {{
    "SPACE_SCHEMA field": "Select ONE location level from SPACE_SCHEMA (location_name, micro_market, city_name, state_name, country_name)",
    "time_period": "Specify exact year, quarter, or date range (e.g., 2024 Q1, January 2024)",
    "transaction_category": "Specify: sale, lease, ownership_transfer, or mortgage",
    "property_type": "Specify: residential, commercial, industrial, or mixed-use"
  }}
}}

Success Response Format (when ALL validations pass):
{{
  "OUTPUT_JSON_SCHEMA": {{
    "analysis_type": "",
    "intent": "",
    "metrics": "",
    "entities": {{}},
    "expected output": "",
    "needs_clarification": false,
    "clarification_question": ""
  }},
  "MAPPED_JSON_SCHEMA": {{
    "analysis_type": "",
    "intent": "",
    "metrics": "",
    "entities": {{
      "space_field": "location_name",
      "property_type": "property_type",
      "time_period": "year",
      "transaction_category": "transaction_category"
    }},
    "expected output": "",
    "needs_clarification": false,
    "clarification_question": ""
  }}
}}
"""

# print("INTENT_EXTRACT_PROMPT:", INTENT_EXTRACT_PROMPT)


def parse_json(text: str, default: Any) -> Any:
    """
    Parse JSON from an LLM response.

    This mirrors the helper used by the real transaction agent: it strips
    markdown fences, tries a full JSON parse, then falls back to the first
    JSON object found in the response.
    """
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


class TransactionStage1IntentExtractor:
    """
    Converts a raw transaction query into the structured Stage 1 intent dict.

    This is copied into a standalone sample shape so it can be run without
    importing the live data_retrieval_transaction modules.
    """

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self.client = client
        self.model = model
        self.last_usage: Any = None

    def extract(self, user_query: str, history: list[dict] | None = None) -> dict:
        prompt = INTENT_EXTRACT_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            space_schema=SPACE_SCHEMA,
            user_query=user_query,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract structured query intent from natural language. "
                    "Use conversation history to resolve pronouns and follow-up requests. "
                    "Example: if user previously asked for 'Baner' and now says 'add total sales', "
                    "the intent should include both 'Baner' and 'total_sales_value'. "
                    "Respond only with valid JSON."
                ),
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
        
        # Print the complete prompt
    
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            timeout=20,
        )
        self.last_usage = response.usage
        raw = response.choices[0].message.content.strip()
        
        # Print raw response from LLM
        print("\n" + "="*80)
        print("RAW LLM RESPONSE:")
        print("="*80)
        print(raw)
        print("="*80 + "\n")
        
        intent = parse_json(raw, default=None)

        if intent is None:
            raise ValueError(
                f"TransactionStage1IntentExtractor failed to parse LLM response: {raw[:300]}"
            )
        
        # Check if this is a clarification response
        if intent.get("needs_clarification") == True:
            print("\n" + "="*80)
            print("CLARIFICATION REQUIRED:")
            print("="*80)
            print(json.dumps(intent, indent=2))
            print("="*80 + "\n")
        else:
            # Extract OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA for success response
            output_schema = intent.get("OUTPUT_JSON_SCHEMA")
            mapped_schema = intent.get("MAPPED_JSON_SCHEMA")
            
            # Print OUTPUT_JSON_SCHEMA
            if output_schema:
                print("\n" + "="*80)
                print("OUTPUT_JSON_SCHEMA:")
                print("="*80)
                print(json.dumps(output_schema, indent=2))
                print("="*80 + "\n")
            
            # Print MAPPED_JSON_SCHEMA
            if mapped_schema:
                print("\n" + "="*80)
                print("MAPPED_JSON_SCHEMA:")
                print("="*80)
                print(json.dumps(mapped_schema, indent=2))
                print("="*80 + "\n")

        locations = (intent.get("entities") or {}).get("locations") or []
        metrics = [metric.get("alias") for metric in (intent.get("metrics") or []) if isinstance(metric, dict)]
        logger.info(
            "Stage 1 extracted: analysis_type=%s locations=%s metrics=%s",
            intent.get("analysis_type"),
            [location.get("value") for location in locations if isinstance(location, dict)],
            metrics,
        )
        return intent


class TransactionStage1SampleAgent:
    """Stage-1-only sample wrapper with the same event style as the real agent."""

    def __init__(self, client: OpenAI, model: str = "gpt-4o-mini") -> None:
        self.domain_key = "transaction"
        self.display_name = "Transaction Agent"
        self.intent_extractor = TransactionStage1IntentExtractor(client, model=model)

    def _event(self, event_type: str, content: Any, **kwargs: Any) -> dict:
        payload = {"type": event_type, "content": content}
        payload.update(kwargs)
        return payload

    def execute_stage1_events(self, question: str, history: list[dict] | None = None) -> Iterable[dict]:
        try:
            yield self._event("stage", f"{self.display_name} - Stage 1: Extracting intent and entities...")
            intent = self.intent_extractor.extract(question, history=history)

            if self.intent_extractor.last_usage is not None:
                yield self._event(
                    "token_usage_raw",
                    None,
                    stage_name=f"{self.domain_key}.s1_intent",
                    usage=self.intent_extractor.last_usage,
                )

            yield self._event("intent", intent, agent=self.domain_key)
            yield self._event(
                "debug_trace",
                {
                    "phase": "observe",
                    "step": "intent_extraction",
                    "summary": "Intent extracted by standalone Transaction Stage 1 sample.",
                    "analysis_type": intent.get("analysis_type"),
                    "metrics": [
                        metric.get("alias")
                        for metric in (intent.get("metrics") or [])
                        if isinstance(metric, dict)
                    ],
                    "locations": [
                        location.get("value")
                        for location in (intent.get("entities") or {}).get("locations") or []
                        if isinstance(location, dict)
                    ],
                },
                agent=self.domain_key,
            )

            route = (intent.get("route") or "internal_db").lower()
            if route == "clarify" or intent.get("needs_clarification"):
                questions = intent.get("clarification_questions") or [
                    "Please provide the missing compulsory field using the available space schema."
                ]
                clarification_payload = {
                    "message": intent.get("clarification_reason")
                    or "I need a little more detail to answer safely.",
                    "questions": questions,
                    "space_schema": SPACE_SCHEMA.strip(),
                }

                yield self._event(
                    "clarification",
                    clarification_payload,
                    agent=self.domain_key,
                    route=route,
                )

        except Exception as error:
            yield self._event("error", f"{self.display_name} Stage 1 failed: {str(error)}")


def run_stage1(question: str, model: str = "gpt-4o-mini", api_key: str | None = None) -> list[dict]:
    """Convenience function for scripts/tests."""
    load_project_env()
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_ADMIN_KEY")
    if not resolved_api_key:
        raise RuntimeError(
            "Missing OpenAI credentials. Add OPENAI_API_KEY to the project .env file, "
            "set it in your shell, or pass --api-key when running this sample."
        )
    client = OpenAI(api_key=resolved_api_key)
    agent = TransactionStage1SampleAgent(client=client, model=model)
    return list(agent.execute_stage1_events(question))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run standalone Transaction Agent Stage 1 sample.")
    parser.add_argument("question", help="Natural language transaction query")
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI model for intent extraction")
    parser.add_argument("--api-key", default=None, help="Optional OpenAI API key. Defaults to OPENAI_API_KEY from .env.")
    args = parser.parse_args()

    for event in run_stage1(args.question, model=args.model, api_key=args.api_key):
        print(json.dumps(event, indent=2, default=str))


if __name__ == "__main__":
    main()
