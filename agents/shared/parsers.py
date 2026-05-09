import ast
import json
import re
from typing import Any


def extract_text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return json.dumps(content, default=str)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(str(item["content"]))
                else:
                    parts.append(json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def try_parse_payload(text_value: str):
    raw = (text_value or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return ast.literal_eval(raw)
    except Exception:
        return None


def collect_agent_trace(messages: list[Any]) -> list[dict]:
    trace = []
    for index, msg in enumerate(messages):
        trace.append(
            {
                "index": index,
                "type": type(msg).__name__,
                "name": getattr(msg, "name", None),
                "content_preview": extract_text_from_content(
                    getattr(msg, "content", "")
                )[:1500],
            }
        )
    return trace


def extract_json_object_from_react_result(agent_result: dict, required_keys: set[str]) -> dict:
    for msg in reversed(agent_result.get("messages", [])):
        content = extract_text_from_content(getattr(msg, "content", ""))
        parsed = try_parse_payload(content)
        if isinstance(parsed, dict) and required_keys.issubset(parsed.keys()):
            return parsed

        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if match:
            parsed = try_parse_payload(match.group(0))
            if isinstance(parsed, dict) and required_keys.issubset(parsed.keys()):
                return parsed

    agent_trace = collect_agent_trace(agent_result.get("messages", []))
    raise RuntimeError(
        "Agent did not return the required JSON object. "
        f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
    )


def last_agent_text(agent_result: dict) -> str:
    for msg in reversed(agent_result.get("messages", [])):
        content = extract_text_from_content(getattr(msg, "content", ""))
        if content.strip():
            return content
    return ""


def extract_jsx_from_payload(payload: Any) -> str:
    if payload is None:
        return ""

    if isinstance(payload, dict):
        for key in ("jsx", "html"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for value in payload.values():
            found = extract_jsx_from_payload(value)
            if found:
                return found
        return ""

    if isinstance(payload, list):
        for item in payload:
            found = extract_jsx_from_payload(item)
            if found:
                return found
        return ""

    if isinstance(payload, str):
        text_value = payload.strip()
        if not text_value:
            return ""

        code_match = re.search(
            r"```jsx?\s*(.*?)```",
            text_value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if code_match:
            candidate = code_match.group(1).strip()
            if candidate:
                return candidate

        if "\nexport" in text_value or "export default" in text_value or "return (" in text_value:
            return text_value

        parsed = try_parse_payload(text_value)
        if parsed is not None:
            return extract_jsx_from_payload(parsed)

    return ""
