from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.tools import tool
from sqlalchemy.orm import Session

from tools.connector.tools import _gmail_failure_response
from database.connector import crud
from utils.connector.gmail_client import GmailAPIClient, GmailAPIError
from agents.connector.services.google_oauth import (
    GMAIL_SYSTEM,
    GOOGLE_PROVIDER,
    refresh_google_access_token,
)


@tool
async def gmail_search_threads(
    access_token: str,
    query: str = "",
    max_results: int = 10,
    trace=None,
) -> dict[str, object]:
    """Search Gmail threads using the connected account."""

    client = GmailAPIClient()
    return await client.search_threads(
        access_token,
        query=query,
        max_results=max_results,
        trace=trace,
    )


@tool
async def gmail_get_thread(
    access_token: str,
    thread_id: str,
    trace=None,
) -> dict[str, object]:
    """Fetch a Gmail thread by thread ID."""

    client = GmailAPIClient()
    return await client.get_thread(access_token, thread_id, trace=trace)


@tool
async def gmail_read_message(
    access_token: str,
    message_id: str,
    trace=None,
) -> dict[str, object]:
    """Read a Gmail message by message ID."""

    client = GmailAPIClient()
    return await client.read_message(access_token, message_id, trace=trace)


@tool
async def gmail_draft_email(
    access_token: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    trace=None,
) -> dict[str, object]:
    """Create a Gmail draft email."""

    client = GmailAPIClient()
    return await client.create_draft(
        access_token,
        to=to,
        subject=subject,
        body=body,
        thread_id=thread_id,
        trace=trace,
    )


@tool
async def gmail_send_email(
    access_token: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    trace=None,
) -> dict[str, object]:
    """Send a Gmail email message."""

    client = GmailAPIClient()
    return await client.send_email(
        access_token,
        to=to,
        subject=subject,
        body=body,
        thread_id=thread_id,
        trace=trace,
    )


@tool
async def gmail_reply_to_thread(
    access_token: str,
    thread_id: str,
    to: str | None = None,
    subject: str | None = None,
    body: str = "",
    trace=None,
) -> dict[str, object]:
    """Reply to an existing Gmail thread."""

    client = GmailAPIClient()
    return await client.reply_to_thread(
        access_token,
        thread_id=thread_id,
        to=to,
        subject=subject,
        body=body,
        trace=trace,
    )


async def _refresh_if_expired(
    db: Session,
    user_id: int,
    oauth_connection,
) -> tuple[Any | None, bool]:
    now = datetime.now(timezone.utc)
    expires_at = oauth_connection.expires_at
    if expires_at is None or expires_at > now:
        return oauth_connection, False

    refreshed = await _refresh_oauth_connection(db, user_id, oauth_connection)
    if refreshed is None or not refreshed.access_token:
        return None, False

    return refreshed, True


async def _refresh_oauth_connection(db: Session, user_id: int, oauth_connection):
    if not oauth_connection.refresh_token:
        return None

    try:
        refreshed = await refresh_google_access_token(oauth_connection.refresh_token)
    except Exception:
        return None

    refreshed_access_token = str(refreshed.get("access_token") or "")
    if not refreshed_access_token:
        return None

    now = datetime.now(timezone.utc)
    refreshed_expires_in = int(refreshed.get("expires_in") or 0)
    refreshed_expires_at = None
    if refreshed_expires_in > 0:
        refreshed_expires_at = now + timedelta(seconds=refreshed_expires_in)

    refreshed_refresh_token = str(refreshed.get("refresh_token") or oauth_connection.refresh_token)

    return crud.upsert_oauth_connection(
        db,
        user_id=user_id,
        provider=GOOGLE_PROVIDER,
        system=GMAIL_SYSTEM,
        email=oauth_connection.email,
        access_token=refreshed_access_token,
        refresh_token=refreshed_refresh_token,
        token_type=str(refreshed.get("token_type")) if refreshed.get("token_type") else oauth_connection.token_type,
        expires_at=refreshed_expires_at,
        scope=str(refreshed.get("scope")) if refreshed.get("scope") else oauth_connection.scope,
    )


async def _fallback_to_draft(
    gmail_client: GmailAPIClient,
    tool_name: str,
    access_token: str,
    arguments: dict[str, object],
    *,
    trace=None,
) -> dict[str, Any] | None:
    if tool_name not in {"gmail.send_email", "gmail.reply_to_thread"}:
        return None

    try:
        return await gmail_client.create_draft(
            access_token,
            to=str(arguments.get("to") or ""),
            subject=str(arguments.get("subject") or ""),
            body=str(arguments.get("body") or ""),
            thread_id=str(arguments.get("thread_id") or "") or None,
            trace=trace,
        )
    except GmailAPIError:
        return None


def _oauth_required_response(server_name: str, tool_name: str) -> dict[str, Any]:
    return _gmail_failure_response(
        server_name,
        tool_name,
        "Gmail is not connected. Please connect Google OAuth first.",
        requires_oauth=True,
    )


def _gmail_api_error_response(server_name: str, tool_name: str, exc: GmailAPIError) -> dict[str, Any]:
    if exc.status_code == 403:
        message = f"Gmail API permission missing or scope is insufficient: {exc}"
        return _gmail_failure_response(server_name, tool_name, message, requires_oauth=True)

    if exc.status_code == 400:
        return _gmail_failure_response(server_name, tool_name, f"Gmail API rejected the request: {exc}")

    if exc.status_code == 401:
        return _gmail_failure_response(
            server_name,
            tool_name,
            "Gmail access token was rejected. Please reconnect Google OAuth.",
            requires_oauth=True,
        )

    return _gmail_failure_response(server_name, tool_name, str(exc))
