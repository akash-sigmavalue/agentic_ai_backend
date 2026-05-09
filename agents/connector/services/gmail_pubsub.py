from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from agents.connector.services.google_oauth import GMAIL_SYSTEM, GOOGLE_PROVIDER
from core.config import settings
from database.connector import models,crud
from utils.connector.gmail_client import GmailAPIClient


class GmailPubSubService:
    """Helper for Gmail push notification registration and incremental sync."""

    def __init__(self, gmail_client: GmailAPIClient | None = None) -> None:
        self._gmail_client = gmail_client or GmailAPIClient()

    async def ensure_watch(self, db: Session, *, connection, connector_id: str | None = None) -> dict[str, Any] | None:
        if not settings.GMAIL_PUBSUB_ENABLE:
            return None
        if not settings.GMAIL_PUBSUB_TOPIC_NAME or not connection.access_token:
            return None

        topic_name = self._normalize_topic_name(settings.GMAIL_PUBSUB_TOPIC_NAME)
        watch_result = await self._gmail_client.watch_mailbox(
            connection.access_token,
            topic_name=topic_name,
            label_ids=["INBOX"],
        )
        history_id = str(watch_result.get("historyId") or "")
        expiration_ms = int(watch_result.get("expiration") or 0)
        watch_expiration = None
        if expiration_ms > 0:
            watch_expiration = datetime.fromtimestamp(expiration_ms / 1000.0, tz=timezone.utc)

        crud.update_oauth_watch_state(
            db,
            user_id=connection.user_id,
            provider=GOOGLE_PROVIDER,
            system=GMAIL_SYSTEM,
            gmail_connector_id=str(connector_id or connection.gmail_connector_id or "") or None,
            gmail_history_id=history_id or connection.gmail_history_id,
            gmail_watch_expiration=watch_expiration,
        )
        return watch_result

    async def stop_watch(self, db: Session, *, connection) -> dict[str, Any] | None:
        if not connection.access_token:
            return None

        result = await self._gmail_client.stop_watch(connection.access_token)
        crud.clear_oauth_watch_state(
            db,
            user_id=connection.user_id,
            provider=GOOGLE_PROVIDER,
            system=GMAIL_SYSTEM,
        )
        return result

    async def renew_expiring_watches(self, db: Session) -> list[dict[str, Any]]:
        if not settings.GMAIL_PUBSUB_ENABLE:
            return []

        threshold = datetime.now(timezone.utc) + timedelta(hours=12)
        expiring_connections = (
            db.query(models.OAuthConnection)
            .filter(
                models.OAuthConnection.provider == GOOGLE_PROVIDER,
                models.OAuthConnection.system == GMAIL_SYSTEM,
                models.OAuthConnection.access_token.isnot(None),
                models.OAuthConnection.gmail_watch_expiration.isnot(None),
                models.OAuthConnection.gmail_watch_expiration <= threshold,
            )
            .all()
        )

        renewed: list[dict[str, Any]] = []
        for connection in expiring_connections:
            watch_result = await self.ensure_watch(db, connection=connection, connector_id=connection.gmail_connector_id)
            if watch_result is not None:
                renewed.append({
                    "user_id": connection.user_id,
                    "email": connection.email,
                    "watch_result": watch_result,
                })

        return renewed

    async def sync_from_history(
        self,
        db: Session,
        *,
        connection,
        start_history_id: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not start_history_id:
            return [], connection.gmail_history_id

        history_response = await self._gmail_client.list_history(
            connection.access_token,
            start_history_id=start_history_id,
            history_types=["messageAdded"],
        )
        latest_history_id = str(history_response.get("historyId") or start_history_id)
        messages = self._extract_history_messages(history_response)
        normalized_messages: list[dict[str, Any]] = []

        for item in messages:
            message = item.get("message") if isinstance(item, dict) else None
            if not isinstance(message, dict):
                continue
            thread_id = str(message.get("threadId") or "")
            message_id = str(message.get("id") or "")
            if not thread_id or not message_id:
                continue

            thread = await self._gmail_client.get_thread(connection.access_token, thread_id)
            normalized_messages.append(self._normalize_thread(thread, message_id=message_id))

        crud.update_oauth_watch_state(
            db,
            user_id=connection.user_id,
            provider=GOOGLE_PROVIDER,
            system=GMAIL_SYSTEM,
            gmail_history_id=latest_history_id,
        )
        return normalized_messages, latest_history_id

    def _normalize_thread(self, thread: dict[str, Any], *, message_id: str | None = None) -> dict[str, Any]:
        messages = thread.get("messages") if isinstance(thread, dict) else None
        latest_message = messages[-1] if isinstance(messages, list) and messages else {}
        if not isinstance(latest_message, dict):
            latest_message = {}

        return {
            "message_id": message_id or thread.get("id"),
            "thread_id": thread.get("id"),
            "sender": self._extract_header(latest_message, "From"),
            "subject": self._extract_header(latest_message, "Subject"),
            "body": thread.get("snippet") or self._extract_body(latest_message),
            "has_attachment": self._has_attachment(latest_message),
            "is_unread": True,
            "thread": thread,
        }

    def _extract_history_messages(self, history_response: dict[str, Any]) -> list[dict[str, Any]]:
        history_items = history_response.get("history")
        if not isinstance(history_items, list):
            return []

        messages: list[dict[str, Any]] = []
        for item in history_items:
            if not isinstance(item, dict):
                continue
            for entry in item.get("messagesAdded") or []:
                if isinstance(entry, dict):
                    messages.append(entry)
        return messages

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

    def _extract_body(self, message: dict[str, Any]) -> str | None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return None
        parts = payload.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                body = part.get("body")
                if isinstance(body, dict):
                    data = body.get("data")
                    if isinstance(data, str) and data.strip():
                        return data.strip()
        body = payload.get("body")
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, str) and data.strip():
                return data.strip()
        return None

    def _has_attachment(self, message: dict[str, Any]) -> bool:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return False
        parts = payload.get("parts")
        if not isinstance(parts, list):
            return False
        return any(isinstance(part, dict) and part.get("filename") for part in parts)

    def _normalize_topic_name(self, topic_name: str) -> str:
        if topic_name.startswith("projects/"):
            return topic_name
        project_id = settings.GMAIL_PUBSUB_PROJECT_ID or ""
        if not project_id:
            return topic_name
        return f"projects/{project_id}/topics/{topic_name}"
