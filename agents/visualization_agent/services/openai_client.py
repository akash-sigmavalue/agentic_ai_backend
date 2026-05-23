"""
Visualization Agent Module 1 — OpenAI API client, cost calculation, token extraction.
"""

import os
import time
from typing import Any, Dict, Tuple

from openai import OpenAI

from .constants import MODEL_PRICING_USD_PER_1M_TOKENS
from .helpers import extract_json_from_text
from .prompts import build_system_prompt, build_user_prompt
from .repair import validate_and_repair_output


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> Dict[str, float]:
    pricing = MODEL_PRICING_USD_PER_1M_TOKENS.get(model)
    if not pricing:
        return {
            "input_cost": 0.0,
            "cached_input_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
        }

    billable_input_tokens = max(input_tokens - cached_input_tokens, 0)
    input_cost = (billable_input_tokens / 1_000_000) * pricing["input"]
    cached_input_cost = (cached_input_tokens / 1_000_000) * pricing["cached_input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    return {
        "input_cost": round(input_cost, 8),
        "cached_input_cost": round(cached_input_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(input_cost + cached_input_cost + output_cost, 8),
    }


def extract_usage_from_response(response: Any) -> Dict[str, int]:
    """Defensive token extraction for OpenAI Responses API usage objects."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
        }

    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens) or (
        input_tokens + output_tokens
    )

    cached_input_tokens = 0
    input_token_details = getattr(usage, "input_tokens_details", None)
    if input_token_details:
        cached_input_tokens = getattr(input_token_details, "cached_tokens", 0) or 0

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }


def call_openai_for_intent(
    user_query: str, model: str
) -> Tuple[Dict[str, Any], Dict[str, int], Dict[str, float], float]:
    """
    Call OpenAI Responses API and return (repaired_output, usage_data, cost_data, elapsed_seconds).
    API key is read from the OPENAI_API_KEY environment variable.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. Add it to your .env file or environment variable."
        )

    client = OpenAI(api_key=api_key)

    start_time = time.time()

    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_user_prompt(user_query)},
        ],
    )

    elapsed = round(time.time() - start_time, 2)

    response_text = response.output_text
    raw_output = extract_json_from_text(response_text)
    repaired_output = validate_and_repair_output(raw_output, user_query)

    usage_data = extract_usage_from_response(response)
    cost_data = calculate_cost(
        model=model,
        input_tokens=usage_data["input_tokens"],
        output_tokens=usage_data["output_tokens"],
        cached_input_tokens=usage_data["cached_input_tokens"],
    )

    return repaired_output, usage_data, cost_data, elapsed
