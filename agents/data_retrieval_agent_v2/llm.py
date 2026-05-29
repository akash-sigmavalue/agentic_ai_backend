import json
import logging
import re
from typing import Any, Protocol

from openai import AsyncOpenAI

from .config import Settings
from .models import SupportedModel

logger = logging.getLogger(__name__)

BEDROCK_CHAT_MODELS = frozenset({"deepseek.v3.2", "moonshotai.kimi-k2.5"})


class JsonAgent(Protocol):
    async def complete_json(
        self, stage_name: str, prompt: str, input_context: str | None = None
    ) -> dict[str, Any]: ...


class OpenAIJsonAgent:
    """Runs one prompt stage through an OpenAI-compatible client and parses JSON."""

    def __init__(self, settings: Settings, model: SupportedModel | None = None) -> None:
        self._model = model or settings.openai_model
        self._token_count = {
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "stages": [],
        }
        self._is_bedrock_chat_model = self._model in BEDROCK_CHAT_MODELS
        self._uses_json_mode = not self._is_bedrock_chat_model
        if self._is_bedrock_chat_model:
            if not settings.bedrock_api_key:
                raise ValueError(
                    "BEDROCK_API_KEY is required to run a model through AWS Bedrock."
                )
            base_url = settings.bedrock_base_url or (
                f"https://bedrock-mantle.{settings.bedrock_region}.api.aws/v1"
            )
            self._client = AsyncOpenAI(api_key=settings.bedrock_api_key, base_url=base_url)
        else:
            if not settings.openai_api_key:
                raise ValueError("OPENAI_API_KEY is required to run an OpenAI model.")
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def complete_json(
        self, stage_name: str, prompt: str, input_context: str | None = None
    ) -> dict[str, Any]:
        if self._is_bedrock_chat_model:
            prompt = (
                f"{prompt}\n\n"
                "MANDATORY OUTPUT CONTRACT: Return exactly one JSON object only. "
                "Do not add reasoning, markdown, explanation, or alternate schemas. "
                "Preserve every required top-level key and its spelling exactly as shown "
                "in the requested output JSON structure."
            )
            if input_context is not None:
                prompt = f"{prompt}\n\nINPUT CONTEXT:\n{input_context}"
            # Bedrock Chat Completions examples supply the task as user content.
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = [{"role": "system", "content": prompt}]
            if input_context is not None:
                messages.append({"role": "user", "content": input_context})
        completion_args: dict[str, Any] = {
            "model": self._model,
            "temperature": 0,
            "messages": messages,
        }
        if self._uses_json_mode:
            completion_args["response_format"] = {"type": "json_object"}
        completion = await self._client.chat.completions.create(**completion_args)
        self._record_token_usage(stage_name, completion)
        content = completion.choices[0].message.content
        if self._is_bedrock_chat_model:
            logger.info(
                "%s raw response for %s:\n%s", self._model, stage_name, content or "<empty>"
            )
        if not content:
            raise ValueError(f"{stage_name} returned an empty model response.")
        result = self._parse_json_object(content, stage_name)
        if not isinstance(result, dict):
            raise ValueError(f"{stage_name} returned JSON that is not an object.")
        if self._is_bedrock_chat_model:
            logger.info(
                "%s parsed JSON for %s:\n%s",
                self._model,
                stage_name,
                json.dumps(result, indent=2),
            )
        return result

    @property
    def token_count(self) -> dict[str, Any]:
        return {
            "total_prompt_tokens": self._token_count["total_prompt_tokens"],
            "total_completion_tokens": self._token_count["total_completion_tokens"],
            "total_tokens": self._token_count["total_tokens"],
            "stages": list(self._token_count["stages"]),
        }

    def _record_token_usage(self, stage_name: str, completion: Any) -> None:
        usage = getattr(completion, "usage", None)
        if usage is None:
            return
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
        self._token_count["total_prompt_tokens"] += prompt_tokens
        self._token_count["total_completion_tokens"] += completion_tokens
        self._token_count["total_tokens"] += total_tokens
        self._token_count["stages"].append(
            {
                "stage": stage_name,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
        )

    @staticmethod
    def _parse_json_object(content: str, stage_name: str) -> dict[str, Any]:
        """Accept a JSON object even when a provider wraps it in prose or fences."""
        candidates = [content.strip()]
        candidates.extend(
            match.group(1).strip()
            for match in re.finditer(
                r"```(?:json)?\s*(.*?)\s*```", content, flags=re.IGNORECASE | re.DOTALL
            )
        )

        decoder = json.JSONDecoder()
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed

            for position, character in enumerate(candidate):
                if character != "{":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(candidate[position:])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed

        raise ValueError(f"{stage_name} returned invalid JSON.")
