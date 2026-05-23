from __future__ import annotations

"""
Transaction Intent Extractor
============================
Converts raw natural language queries to structured intent dicts.
"""

import logging
from typing import Any

from openai import OpenAI

from utils.data_retrieval.clarification import SPACE_CLARIFICATION_QUESTION
from agents.data_retrieval_transaction.helpers import (
    intent_has_space_context,
    merge_space_filters,
    parse_json,
)
from agents.data_retrieval_transaction.prompts import INTENT_EXTRACT_PROMPT
from agents.data_retrieval_transaction.schema import TRANSACTION_QUERY_SCHEMA

logger = logging.getLogger(__name__)


def mark_space_clarification_required(intent: dict) -> None:
    """Mark an intent as requiring space clarification from the user."""
    intent["route"] = "clarify"
    intent["needs_clarification"] = True
    intent["clarification_reason"] = (
        "I need to know which space or geography to filter before querying transaction data."
    )
    intent["clarification_questions"] = [SPACE_CLARIFICATION_QUESTION]


class IntentExtractor:
    """
    Converts a raw user query string into a structured intent dict.

    This is the ONLY pre-processing step before SQL generation.
    It faithfully captures everything the user asked for — all entities,
    all metrics, the analysis type — without narrowing or pre-filtering.

    Attributes:
        client: OpenAI API client
        model: Model name to use for LLM calls (default: gpt-5.1)
        last_usage: Token usage from the last extraction call
    """

    def __init__(self, client: OpenAI, model: str = "gpt-5.1") -> None:
        self.client = client
        self.model  = model
        self.last_usage: Any = None

    def extract(self, user_query: str, history: list[dict] | None = None) -> dict:
        """
        Extract structured intent from raw user_query with conversation context.

        Args:
            user_query: The raw natural language query from the user
            history: Optional conversation history for context resolution

        Returns:
            A dict with analysis_type, metrics, entities, filters, group_by,
            order_by, and time_series fields, plus route and clarification info.

        Raises:
            ValueError: If the LLM response cannot be parsed as JSON.
        """
        prompt = INTENT_EXTRACT_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
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
            # Add last 4 messages for context (2 user, 2 assistant)
            for msg in history[-4:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
        
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            timeout=20,
        )
        self.last_usage = response.usage
        raw    = response.choices[0].message.content.strip()
        intent = parse_json(raw, default=None)

        if intent is None:
            raise ValueError(
                f"IntentExtractor: failed to parse LLM response: {raw[:300]}"
            )

        merge_space_filters(intent, user_query)
        if not intent_has_space_context(intent):
            mark_space_clarification_required(intent)

        locations = (intent.get("entities") or {}).get("locations") or []
        metrics   = [m.get("alias") for m in (intent.get("metrics") or [])]

        logger.info(
            "IntentExtractor: analysis_type=%s  locations=%s  metrics=%s",
            intent.get("analysis_type"),
            [loc.get("value") for loc in locations],
            metrics,
        )
        
        # Print intent to terminal for debugging
        import json
        print(f"\n{'='*80}")
        print(f"[TRANSACTION] EXTRACTED INTENT:")
        print(f"{'='*80}")
        print(json.dumps(intent, indent=2))
        print(f"{'='*80}\n", flush=True)
        
        return intent
