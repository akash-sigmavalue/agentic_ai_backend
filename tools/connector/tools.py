from typing import Any


def _resolve_tool_name(system: str, operation: str) -> str:
    """Map internal workflow operation names to Gmail tool names."""

    if system != "gmail":
        raise ValueError("Only Gmail connector is supported currently")

    operation_map = {
        "list_messages": "gmail.search_threads",
        "search_messages": "gmail.search_threads",
        "search_threads": "gmail.search_threads",
        "gmail.search_threads": "gmail.search_threads",
        "get_message": "gmail.get_thread",
        "get_thread": "gmail.get_thread",
        "gmail.get_thread": "gmail.get_thread",
        "read_message": "gmail.read_message",
        "gmail.read_message": "gmail.read_message",
        "create_draft": "gmail.draft_email",
        "draft_email": "gmail.draft_email",
        "gmail.draft_email": "gmail.draft_email",
        "send_email": "gmail.send_email",
        "gmail.send_email": "gmail.send_email",
        "reply_to_thread": "gmail.reply_to_thread",
        "gmail.reply_to_thread": "gmail.reply_to_thread",
    }

    tool_name = operation_map.get(operation)
    if not tool_name:
        raise ValueError(f"Unsupported Gmail operation '{operation}'")

    return tool_name


def _sanitize_args(arguments: dict[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in arguments.items():
        if key in {"body", "content", "text"} and isinstance(value, str):
            sanitized[key] = value if len(value) <= 200 else f"{value[:197].rstrip()}..."
        else:
            sanitized[key] = value
    return sanitized


def _endpoint_for_tool(tool_name: str) -> str:
    endpoint_map = {
        "gmail.search_threads": "https://gmail.googleapis.com/gmail/v1/users/me/threads",
        "gmail.get_thread": "https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
        "gmail.read_message": "https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
        "gmail.draft_email": "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        "gmail.send_email": "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        "gmail.reply_to_thread": "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
    }
    return endpoint_map.get(tool_name, "gmail.googleapis.com")


def _normalize_search_response(data: dict[str, object]) -> dict[str, object]:
    threads = data.get("threads")
    thread_items = threads if isinstance(threads, list) else []
    thread_ids: list[str] = []
    for item in thread_items:
        if isinstance(item, dict):
            thread_id = item.get("id")
            if isinstance(thread_id, str) and thread_id.strip():
                thread_ids.append(thread_id.strip())

    normalized = dict(data)
    normalized["thread_ids"] = thread_ids
    normalized["first_thread_id"] = thread_ids[0] if thread_ids else None
    normalized["threads"] = thread_items
    normalized["messages"] = []
    return normalized


def _normalize_thread_response(data: dict[str, object]) -> dict[str, object]:
    thread_id = data.get("id")
    messages = data.get("messages")
    normalized_messages = messages if isinstance(messages, list) else []
    latest_message = normalized_messages[-1] if normalized_messages else {}
    normalized = dict(data)
    normalized["thread_id"] = thread_id
    normalized["messages"] = normalized_messages
    normalized["message_count"] = len(normalized_messages)
    normalized["subject"] = _extract_message_header(latest_message, "Subject")
    normalized["from_email"] = _extract_message_header(latest_message, "From")
    normalized["snippet"] = data.get("snippet")
    return normalized


def _normalize_message_response(data: dict[str, object]) -> dict[str, object]:
    normalized = dict(data)
    normalized["message_id"] = data.get("id")
    normalized["thread_id"] = data.get("threadId")
    payload = data.get("payload")
    headers: list[dict[str, object]] = []
    if isinstance(payload, dict):
        raw_headers = payload.get("headers")
        if isinstance(raw_headers, list):
            headers = [item for item in raw_headers if isinstance(item, dict)]
    normalized["headers"] = headers
    normalized["from_email"] = _extract_message_header(data, "From")
    normalized["subject"] = _extract_message_header(data, "Subject")
    normalized["snippet"] = data.get("snippet")
    return normalized


def _extract_message_header(message: dict[str, object], header_name: str) -> str | None:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return None
    for header in headers:
        if not isinstance(header, dict):
            continue
        if str(header.get("name") or "").lower() == header_name.lower():
            value = header.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _gmail_failure_response(
    server_name: str,
    tool_name: str,
    message: str,
    *,
    requires_oauth: bool = False,
) -> dict[str, Any]:
    return {
        "server": server_name,
        "tool": tool_name,
        "ok": False,
        "error": message,
        "requires_oauth": requires_oauth,
        "connector": "gmail",
    }


def _record_connector_trace(trace, data: dict[str, Any]) -> None:
    return None
