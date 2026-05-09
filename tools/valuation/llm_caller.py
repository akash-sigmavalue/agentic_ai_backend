"""
Centralized LLM call utility for PropVal India.
- GPT-4o, temperature=0.1, response_format=json_object
- Retry on JSON parse error, missing fields, rate limits
- 30-second timeout, max 2 retries
"""

import json
import time
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))


def get_client() -> OpenAI:
    return _client


def call_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
    temperature: float = 0.1,
    retries: int = 2,
    tools: list = None,
    tool_functions: dict = None,
) -> dict:
    """
    Make a structured JSON call to GPT-4o with automatic retry.
    Supports OpenAI tools (function calling) natively.

    Returns:
        dict with keys: "data" (parsed JSON), "usage" (token usage object), "success" (bool)
    """
    last_error = None

    for attempt in range(retries + 1):
        try:
            extra_instruction = ""
            if attempt > 0:
                extra_instruction = "\n\nIMPORTANT: Return ONLY valid JSON. No prose, no markdown fences."

            messages = [
                {"role": "system", "content": system_prompt + extra_instruction},
                {"role": "user", "content": user_prompt},
            ]
            
            # Keep looping to handle tool calls
            while True:
                kwargs = {
                    "model": "gpt-4o-mini",
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "timeout": 30,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools

                response = _client.chat.completions.create(**kwargs)
                message = response.choices[0].message
                
                # If LLM decides to call a tool
                if message.tool_calls:
                    messages.append(message)
                    for tool_call in message.tool_calls:
                        func_name = tool_call.function.name
                        try:
                            func_args = json.loads(tool_call.function.arguments)
                        except json.JSONDecodeError:
                            func_args = {}
                            
                        if tool_functions and func_name in tool_functions:
                            try:
                                result = tool_functions[func_name](**func_args)
                            except Exception as e:
                                result = {"error": str(e)}
                        else:
                            result = {"error": f"Unknown function {func_name}"}
                            
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": func_name,
                            "content": json.dumps(result)
                        })
                    # Loop back to let the LLM use the tool results
                    continue
                else:
                    # Final text response
                    raw = message.content
                    parsed = json.loads(raw)

                    return {
                        "data": parsed,
                        "raw": raw,
                        "usage": response.usage,
                        "model": response.model,
                        "success": True,
                    }

        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {str(e)}"
            continue

        except Exception as e:
            error_str = str(e)
            # Rate limit — exponential backoff
            if "429" in error_str or "rate_limit" in error_str.lower():
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
                last_error = f"Rate limit hit, waited {wait}s"
                continue
            last_error = error_str
            continue

    return {
        "data": {},
        "usage": None,
        "model": "gpt-4o-mini",
        "success": False,
        "error": last_error,
    }


def call_llm_text(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 3000,
    temperature: float = 0.3,
) -> dict:
    """
    Make a text (non-JSON) call to GPT-4o for report generation.
    """
    try:
        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return {
            "data": response.choices[0].message.content,
            "usage": response.usage,
            "model": response.model,
            "success": True,
        }
    except Exception as e:
        return {
            "data": "",
            "usage": None,
            "model": "gpt-4o-mini",
            "success": False,
            "error": str(e),
        }

