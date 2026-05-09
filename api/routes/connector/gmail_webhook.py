from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from agents.connector.services.gmail_pubsub import GmailPubSubService
from agents.connector.services.gmail_trigger_service import GmailTriggerService
from core.config import settings
from database.connector import crud
from database import db
from utils.connector.gmail_client import GmailAPIClient


router = APIRouter(prefix="/webhooks", tags=["gmail-webhooks"])


def _decode_pubsub_data(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        if isinstance(payload.get("emailAddress"), str):
            return payload
        message = payload.get("message")
        if isinstance(message, dict):
            data = message.get("data")
            if isinstance(data, str) and data.strip():
                decoded = _decode_base64_json(data)
                if isinstance(decoded, dict):
                    return decoded
    return {}


def _decode_base64_json(value: str) -> Any:
    padded = value + "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


@router.post("/gmail")
async def gmail_webhook(
    request: Request,
    db: Session = Depends(db.get_db),
    token: str | None = Header(default=None, alias="X-PubSub-Token"),
) -> dict[str, Any]:
    if settings.GMAIL_PUBSUB_WEBHOOK_TOKEN and token != settings.GMAIL_PUBSUB_WEBHOOK_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Gmail webhook token")

    payload = await request.json()
    event = _decode_pubsub_data(payload)

    email_address = str(event.get("emailAddress") or event.get("email_address") or "").strip()
    history_id = str(event.get("historyId") or event.get("history_id") or "").strip()
    if not email_address:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="emailAddress is required")

    connection = crud.get_oauth_connection_by_email(
        db,
        provider="google",
        system="gmail",
        email=email_address,
    )
    if connection is None:
        return {
            "success": True,
            "status": "ignored",
            "reason": "No Gmail OAuth connection found for the mailbox",
            "emailAddress": email_address,
            "historyId": history_id,
        }

    gmail_service = GmailPubSubService(GmailAPIClient())
    trigger_service = GmailTriggerService()

    stored_history_id = str(connection.gmail_history_id or "").strip()
    if not stored_history_id:
        crud.update_oauth_watch_state(
            db,
            user_id=connection.user_id,
            provider="google",
            system="gmail",
            gmail_history_id=history_id or None,
        )
        await gmail_service.ensure_watch(db, connection=connection, connector_id=connection.gmail_connector_id)
        return {
            "success": True,
            "status": "watch_initialized",
            "emailAddress": email_address,
            "historyId": history_id,
        }

    if not history_id:
        return {
            "success": True,
            "status": "ignored",
            "reason": "Missing historyId",
            "emailAddress": email_address,
        }

    normalized_messages, latest_history_id = await gmail_service.sync_from_history(
        db,
        connection=connection,
        start_history_id=stored_history_id,
    )

    results: list[dict[str, Any]] = []
    for email_item in normalized_messages:
        result = await trigger_service.process_email_event(
            db,
            connection=connection,
            email_payload={
                **email_item,
                "received_at": payload.get("message", {}).get("publishTime") if isinstance(payload.get("message"), dict) else None,
                "history_id": latest_history_id or history_id,
            },
            stream_key=connection.gmail_connector_id or str(connection.id),
        )
        results.append(result)

    if not normalized_messages:
        crud.update_oauth_watch_state(
            db,
            user_id=connection.user_id,
            provider="google",
            system="gmail",
            gmail_history_id=history_id,
        )

    return {
        "success": True,
        "status": "processed" if results else "no_new_messages",
        "emailAddress": email_address,
        "historyId": history_id,
        "processed_count": len(results),
        "results": results,
    }
