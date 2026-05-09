from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from agents.connector.services.google_oauth import GMAIL_SYSTEM, GOOGLE_PROVIDER
from api.schemas.connector.request_models import ConnectorTaskRequest
from tools.connector.tools1 import (
    _fallback_to_draft,
    _gmail_api_error_response,
    _oauth_required_response,
    _refresh_if_expired,
    _refresh_oauth_connection,
    gmail_draft_email,
    gmail_get_thread,
    gmail_read_message,
    gmail_reply_to_thread,
    gmail_search_threads,
    gmail_send_email,
)
from tools.connector.tools import (
    _endpoint_for_tool,
    _gmail_failure_response,
    _normalize_message_response,
    _normalize_search_response,
    _normalize_thread_response,
    _record_connector_trace,
    _resolve_tool_name,
    _sanitize_args,
)
from database.connector import crud
from utils.connector.gmail_client import GmailAPIClient, GmailAPIError


logger = logging.getLogger(__name__)


class ConnectorAgent:
    """Routes structured connector work directly to the Gmail API backend."""

    MAX_RETRIES = 3

    def __init__(
        self,
        gmail_client: GmailAPIClient | None = None,
    ) -> None:
        self._gmail_client = gmail_client or GmailAPIClient()

    async def execute(
        self,
        task: ConnectorTaskRequest,
        db: Session | None = None,
        user_id: int | None = None,
        trace=None,
    ) -> dict[str, Any]:
        if task.system != GMAIL_SYSTEM:
            raise ValueError("Only Gmail connector is supported currently")

        return await self._execute_gmail_task(task, db=db, user_id=user_id, trace=trace)

    async def _execute_gmail_task(
        self,
        task: ConnectorTaskRequest,
        db: Session | None = None,
        user_id: int | None = None,
        trace=None,
    ) -> dict[str, Any]:
        server_name = "google-gmail-api"
        tool_name = _resolve_tool_name(task.system, task.operation)
        arguments = dict(task.input or {})
        debug_payload = {
            "connector_used": GMAIL_SYSTEM,
            "api_used": "google_gmail_api",
            "endpoint_used": None,
            "oauth_connection_found": False,
            "google_account_email": None,
            "token_refreshed": False,
            "selected_tool": tool_name,
            "sanitized_request_args": _sanitize_args(arguments),
            "status": "starting",
            "error": None,
        }
        _record_connector_trace(trace, debug_payload)
        logger.info("connector agent started for %s", tool_name)

        if db is None or user_id is None:
            debug_payload["status"] = "oauth_required"
            debug_payload["error"] = "Gmail OAuth connection missing"
            debug_payload["endpoint_used"] = _endpoint_for_tool(tool_name)
            _record_connector_trace(trace, debug_payload)
            return _oauth_required_response(server_name, tool_name)

        oauth_connection = crud.get_oauth_connection(
            db,
            user_id=user_id,
            provider=GOOGLE_PROVIDER,
            system=GMAIL_SYSTEM,
        )
        if oauth_connection is None or not oauth_connection.access_token:
            debug_payload["status"] = "oauth_required"
            debug_payload["error"] = "Gmail OAuth connection missing"
            debug_payload["endpoint_used"] = _endpoint_for_tool(tool_name)
            _record_connector_trace(trace, debug_payload)
            return _oauth_required_response(server_name, tool_name)

        debug_payload["oauth_connection_found"] = True
        debug_payload["google_account_email"] = oauth_connection.email
        oauth_connection, token_refreshed = await _refresh_if_expired(db, user_id, oauth_connection)
        debug_payload["token_refreshed"] = token_refreshed
        if oauth_connection is None or not oauth_connection.access_token:
            debug_payload["status"] = "oauth_required"
            debug_payload["error"] = "Gmail OAuth refresh failed"
            debug_payload["endpoint_used"] = _endpoint_for_tool(tool_name)
            _record_connector_trace(trace, debug_payload)
            return _oauth_required_response(server_name, tool_name)

        try:
            data = await self._call_gmail_operation_with_retries(
                tool_name,
                oauth_connection.access_token,
                arguments,
                db=db,
                user_id=user_id,
                trace=trace,
            )
            api_metadata = data.pop("api_metadata", None) if isinstance(data, dict) else None
            if isinstance(api_metadata, dict):
                debug_payload["endpoint_used"] = api_metadata.get("endpoint")
                debug_payload["status"] = "success"
            else:
                debug_payload["endpoint_used"] = _endpoint_for_tool(tool_name)
                debug_payload["status"] = "success"
            _record_connector_trace(trace, debug_payload)
        except GmailAPIError as exc:
            if exc.status_code == 401:
                oauth_connection = await _refresh_oauth_connection(db, user_id, oauth_connection)
                if oauth_connection is None or not oauth_connection.access_token:
                    debug_payload["status"] = "failed"
                    debug_payload["error"] = "Gmail access token was rejected. Please reconnect Google OAuth."
                    _record_connector_trace(trace, debug_payload)
                    return _gmail_failure_response(
                        server_name,
                        tool_name,
                        "Gmail access token was rejected. Please reconnect Google OAuth.",
                        requires_oauth=True,
                    )

                try:
                    debug_payload["token_refreshed"] = True
                    data = await self._call_gmail_operation_with_retries(
                        tool_name,
                        oauth_connection.access_token,
                        arguments,
                        db=db,
                        user_id=user_id,
                        trace=trace,
                    )
                    api_metadata = data.pop("api_metadata", None) if isinstance(data, dict) else None
                    if isinstance(api_metadata, dict):
                        debug_payload["endpoint_used"] = api_metadata.get("endpoint")
                    else:
                        debug_payload["endpoint_used"] = _endpoint_for_tool(tool_name)
                    debug_payload["status"] = "success"
                    _record_connector_trace(trace, debug_payload)
                except GmailAPIError as retry_exc:
                    fallback = await _fallback_to_draft(
                        self._gmail_client,
                        tool_name,
                        oauth_connection.access_token,
                        arguments,
                        trace=trace,
                    )
                    if fallback is not None:
                        debug_payload["status"] = "degraded"
                        debug_payload["error"] = str(retry_exc)
                        _record_connector_trace(trace, debug_payload)
                        return fallback
                    debug_payload["status"] = "failed"
                    debug_payload["error"] = str(retry_exc)
                    debug_payload["endpoint_used"] = _endpoint_for_tool(tool_name)
                    _record_connector_trace(trace, debug_payload)
                    return _gmail_api_error_response(server_name, tool_name, retry_exc)
            else:
                fallback = await _fallback_to_draft(
                    self._gmail_client,
                    tool_name,
                    oauth_connection.access_token,
                    arguments,
                    trace=trace,
                )
                if fallback is not None:
                    debug_payload["status"] = "degraded"
                    debug_payload["error"] = str(exc)
                    _record_connector_trace(trace, debug_payload)
                    return fallback
                debug_payload["status"] = "failed"
                debug_payload["error"] = str(exc)
                debug_payload["endpoint_used"] = _endpoint_for_tool(tool_name)
                _record_connector_trace(trace, debug_payload)
                return _gmail_api_error_response(server_name, tool_name, exc)

        return {
            "server": server_name,
            "connector": GMAIL_SYSTEM,
            "ok": True,
            "tool": tool_name,
            "data": data,
            "raw_mcp_results": data,
        }

    async def _call_gmail_operation(
        self,
        tool_name: str,
        access_token: str,
        arguments: dict[str, object],
        *,
        trace=None,
    ) -> dict[str, object]:
        if tool_name in {"gmail.search_threads", "search_threads"}:
            data = await gmail_search_threads.ainvoke(
                {
                    "access_token": access_token,
                    "query": str(arguments.get("query") or ""),
                    "max_results": int(arguments.get("max_results") or 10),
                    "trace": trace,
                }
            )
            return _normalize_search_response(data)

        if tool_name in {"gmail.get_thread", "get_thread"}:
            thread_id = str(arguments.get("thread_id") or arguments.get("id") or "")
            data = await gmail_get_thread.ainvoke(
                {
                    "access_token": access_token,
                    "thread_id": thread_id,
                    "trace": trace,
                }
            )
            return _normalize_thread_response(data)

        if tool_name in {"gmail.read_message", "read_message"}:
            message_id = str(arguments.get("message_id") or arguments.get("id") or "")
            data = await gmail_read_message.ainvoke(
                {
                    "access_token": access_token,
                    "message_id": message_id,
                    "trace": trace,
                }
            )
            return _normalize_message_response(data)

        if tool_name in {"gmail.draft_email", "draft_email"}:
            return await gmail_draft_email.ainvoke(
                {
                    "access_token": access_token,
                    "to": str(arguments.get("to") or ""),
                    "subject": str(arguments.get("subject") or ""),
                    "body": str(arguments.get("body") or ""),
                    "thread_id": str(arguments.get("thread_id") or "") or None,
                    "trace": trace,
                }
            )

        if tool_name in {"gmail.send_email", "send_email"}:
            return await gmail_send_email.ainvoke(
                {
                    "access_token": access_token,
                    "to": str(arguments.get("to") or ""),
                    "subject": str(arguments.get("subject") or ""),
                    "body": str(arguments.get("body") or ""),
                    "thread_id": str(arguments.get("thread_id") or "") or None,
                    "trace": trace,
                }
            )

        if tool_name in {"gmail.reply_to_thread", "reply_to_thread"}:
            return await gmail_reply_to_thread.ainvoke(
                {
                    "access_token": access_token,
                    "thread_id": str(arguments.get("thread_id") or ""),
                    "to": str(arguments.get("to") or "") or None,
                    "subject": str(arguments.get("subject") or "") or None,
                    "body": str(arguments.get("body") or ""),
                    "trace": trace,
                }
            )

        raise ValueError(f"Unsupported Gmail operation: {tool_name}")

    async def _call_gmail_operation_with_retries(
        self,
        tool_name: str,
        access_token: str,
        arguments: dict[str, object],
        *,
        db: Session | None = None,
        user_id: int | None = None,
        trace=None,
    ) -> dict[str, object]:
        last_exc: GmailAPIError | None = None
        current_token = access_token

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                return await self._call_gmail_operation(tool_name, current_token, arguments, trace=trace)
            except GmailAPIError as exc:
                last_exc = exc
                if exc.status_code == 401 and db is not None and user_id is not None:
                    oauth_connection = crud.get_oauth_connection(
                        db,
                        user_id=user_id,
                        provider=GOOGLE_PROVIDER,
                        system=GMAIL_SYSTEM,
                    )
                    if oauth_connection is not None:
                        refreshed = await _refresh_oauth_connection(db, user_id, oauth_connection)
                        if refreshed is not None and refreshed.access_token:
                            current_token = refreshed.access_token
                            continue
                if attempt < self.MAX_RETRIES and (exc.status_code >= 500 or exc.status_code == 429):
                    continue
                raise

        if last_exc is not None:
            raise last_exc
        raise GmailAPIError(500, "Unexpected Gmail connector failure")
