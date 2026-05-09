from __future__ import annotations

import base64
import logging
import time
from email.message import EmailMessage
from typing import Any

import httpx


GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"

logger = logging.getLogger(__name__)


class GmailAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class GmailAPIClient:
    """Direct Gmail API client used by the connector agent."""

    async def search_threads(
        self,
        access_token: str,
        query: str = "",
        max_results: int = 10,
        trace=None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query

        return await self._request_json("GET", "/threads", access_token, params=params, trace=trace)

    async def watch_mailbox(
        self,
        access_token: str,
        *,
        topic_name: str,
        label_ids: list[str] | None = None,
        trace=None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topicName": topic_name,
            "labelIds": label_ids or ["INBOX"],
        }
        return await self._request_json("POST", "/watch", access_token, json_body=payload, trace=trace)

    async def stop_watch(self, access_token: str, trace=None) -> dict[str, Any]:
        return await self._request_json("POST", "/stop", access_token, trace=trace)

    async def list_history(
        self,
        access_token: str,
        *,
        start_history_id: str,
        history_types: list[str] | None = None,
        trace=None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"startHistoryId": start_history_id}
        if history_types:
            params["historyTypes"] = history_types
        return await self._request_json("GET", "/history", access_token, params=params, trace=trace)

    async def get_thread(self, access_token: str, thread_id: str, trace=None) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id is required")

        return await self._request_json("GET", f"/threads/{thread_id}", access_token, trace=trace)

    async def read_message(self, access_token: str, message_id: str, trace=None) -> dict[str, Any]:
        if not message_id:
            raise ValueError("message_id is required")

        return await self._request_json("GET", f"/messages/{message_id}", access_token, trace=trace)

    async def create_draft(
        self,
        access_token: str,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        trace=None,
    ) -> dict[str, Any]:
        raw_message = self._build_raw_message(to=to, subject=subject, body=body, thread_id=thread_id)
        payload: dict[str, Any] = {"message": {"raw": raw_message}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        return await self._request_json("POST", "/drafts", access_token, json_body=payload, trace=trace)

    async def send_email(
        self,
        access_token: str,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        trace=None,
    ) -> dict[str, Any]:
        raw_message = self._build_raw_message(to=to, subject=subject, body=body, thread_id=thread_id)
        payload: dict[str, Any] = {"raw": raw_message}
        if thread_id:
            payload["threadId"] = thread_id
        return await self._request_json("POST", "/messages/send", access_token, json_body=payload, trace=trace)

    async def reply_to_thread(
        self,
        access_token: str,
        thread_id: str,
        to: str | None = None,
        subject: str | None = None,
        body: str = "",
        trace=None,
    ) -> dict[str, Any]:
        thread = await self.get_thread(access_token, thread_id, trace=trace)
        latest_message = self._latest_message_from_thread(thread)
        original_sender = to or self._extract_header(latest_message, "From") or ""
        original_subject = subject or self._extract_header(latest_message, "Subject") or ""
        reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}".strip()
        references = self._extract_header(latest_message, "Message-ID")
        in_reply_to = references

        raw_message = self._build_raw_message(
            to=original_sender,
            subject=reply_subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
        )
        payload = {"raw": raw_message, "threadId": thread_id}
        return await self._request_json("POST", "/messages/send", access_token, json_body=payload, trace=trace)

    async def _request_json(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        trace=None,
    ) -> dict[str, Any]:
        if not access_token:
            raise GmailAPIError(401, "Missing Gmail OAuth access token.")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        started_at = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method,
                f"{GMAIL_API_BASE_URL}{path}",
                headers=headers,
                params=params,
                json=json_body,
            )
        duration_ms = int((time.perf_counter() - started_at) * 1000)

        api_metadata = {
            "api_used": "google_gmail_api",
            "method": method,
            "endpoint": f"{GMAIL_API_BASE_URL}{path}",
            "query_params": params or {},
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }
        logger.debug("gmail api request completed: %s", api_metadata)

        if response.status_code >= 400:
            raise GmailAPIError(
                response.status_code,
                self._extract_error_message(response),
                self._extract_error_payload(response),
            )

        if not response.content:
            return {}

        try:
            data = response.json()
        except Exception:
            return {"raw": response.text}

        return data if isinstance(data, dict) else {"result": data}

    def _build_raw_message(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> str:
        message = EmailMessage()
        if to:
            message["To"] = to
        message["Subject"] = subject
        if thread_id:
            message["X-Thread-Id"] = thread_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        return encoded.rstrip("=")

    def _latest_message_from_thread(self, thread: dict[str, Any]) -> dict[str, Any]:
        messages = thread.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                return last
        return {}

    def _extract_header(self, message: dict[str, Any], header_name: str) -> str | None:
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

    def _extract_error_payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {"raw": response.text}

        return payload if isinstance(payload, dict) else {"result": payload}

    def _extract_error_message(self, response: httpx.Response) -> str:
        payload = self._extract_error_payload(response)

        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()

            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        return f"Gmail API returned {response.status_code}"
