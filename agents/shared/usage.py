from __future__ import annotations

from typing import Any


def normalize_usage_metadata(message: Any) -> dict[str, Any] | None:
    if message is None:
        return None

    usage = getattr(message, "usage_metadata", None)
    response_metadata = getattr(message, "response_metadata", None) or {}
    if usage is None:
        usage = response_metadata.get("usage_metadata") or response_metadata.get("token_usage")

    if usage is None:
        return None

    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif hasattr(usage, "dict"):
        usage = usage.dict()

    if not isinstance(usage, dict):
        return None

    normalized = dict(usage)
    total_tokens = normalized.get("total_tokens")
    if total_tokens is None:
        prompt_tokens = normalized.get("input_tokens") or normalized.get("prompt_tokens") or 0
        output_tokens = normalized.get("output_tokens") or normalized.get("completion_tokens") or normalized.get("response_tokens") or 0
        normalized["input_tokens"] = prompt_tokens
        normalized["output_tokens"] = output_tokens
        normalized["total_tokens"] = prompt_tokens + output_tokens
    else:
        normalized.setdefault("input_tokens", normalized.get("prompt_tokens") or normalized.get("input_tokens") or 0)
        normalized.setdefault("output_tokens", normalized.get("completion_tokens") or normalized.get("response_tokens") or normalized.get("output_tokens") or 0)

    model_name = response_metadata.get("model_name") or response_metadata.get("model")
    model_provider = response_metadata.get("model_provider")
    if model_name is not None:
        normalized["model_name"] = model_name
    if model_provider is not None:
        normalized["model_provider"] = model_provider

    return normalized
