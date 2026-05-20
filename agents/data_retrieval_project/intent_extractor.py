from __future__ import annotations

"""
Project Intent Extractor
=======================
Converts raw natural language queries to structured intent dicts for projects.
"""

import logging
from typing import Any

from openai import OpenAI

from agents.data_retrieval_project.helpers import (
    intent_has_space_context,
    mark_space_clarification_required,
    merge_space_filters,
    parse_json,
)
from agents.data_retrieval_project.prompts import INTENT_EXTRACT_PROMPT
from agents.data_retrieval_project.schema import PROJECT_QUERY_SCHEMA

logger = logging.getLogger(__name__)

class IntentExtractor:
    """
    Converts a raw user query string into a structured intent dict.

    This is the ONLY pre-processing step before SQL generation.
    It faithfully captures everything the user asked for — all entities,
    all metrics, the analysis type — without narrowing or pre-filtering.
    """

    def __init__(self, client: OpenAI, model: str = "gpt-5.1") -> None:
        self.client = client
        self.model  = model
        self.last_usage = None

    def extract(self, user_query: str, history: list[dict] | None = None) -> dict:
        """
        Extract structured intent from raw user_query with conversation context.
        """
        prompt = INTENT_EXTRACT_PROMPT.format(
            schema=PROJECT_QUERY_SCHEMA,
            user_query=user_query,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract structured query intent from natural language. "
                    "Use conversation history to resolve pronouns and follow-up requests. "
                    "Example: if user previously asked for 'Baner' and now says 'add total projects', "
                    "the intent should include both 'Baner' and 'project_count'. "
                    "Respond only with valid JSON."
                ),
            },
        ]
        if history:
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
        if intent_has_space_context(intent):
            intent["route"] = "internal_db"
            intent["needs_clarification"] = False
            intent["clarification_reason"] = ""
            intent["clarification_questions"] = []
        else:
            mark_space_clarification_required(intent)

        locations = (intent.get("entities") or {}).get("locations") or []
        metrics   = [m.get("alias") for m in (intent.get("metrics") or [])]

        logger.info(
            "IntentExtractor: analysis_type=%s  locations=%s  metrics=%s",
            intent.get("analysis_type"),
            [loc.get("value") for loc in locations],
            metrics,
        )
        return intent


# ══════════════════════════════════════════════════════════════════════════════

