from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from agents.connector.services.gmail_connector_streams import stream_gmail_connector_events
from agents.connector.services.gmail_pubsub import GmailPubSubService
from agents.connector.services.gmail_trigger_service import GmailTriggerService
from api.schemas.connector.email_processing_log import EmailProcessingLog
# Authentication/authorization is currently disabled.
# from auth.connector.dependencies import get_current_user
from database.connector import crud
from database import db
from database.connector.automation_crud import (
    create_automation_rule,
    get_automation_rules,
    get_automation_execution_logs,
    get_automation_rule_by_id,
    update_automation_rule_status,
    upsert_contact_mapping,
)
from utils.connector.gmail_client import GmailAPIClient

router = APIRouter(prefix="/connectors", tags=["connectors"])
AUTH_DISABLED_USER_ID = 1


@router.get("/status/gmail")
def gmail_status(
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, object]:
    connection = crud.get_oauth_connection(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        provider="google",
        system="gmail",
    )
    watch_active = False
    watch_expires_at = None
    connector_id = None
    history_id = None
    if connection is not None:
        watch_expires_at = connection.gmail_watch_expiration
        history_id = connection.gmail_history_id
        connector_id = connection.gmail_connector_id or str(connection.id)
        watch_active = bool(watch_expires_at is not None and watch_expires_at > datetime.now(timezone.utc))
    return {
        "connected": connection is not None and bool(connection.access_token),
        "email": connection.email if connection else None,
        "connector_id": connector_id,
        "watch_active": watch_active,
        "watch_expires_at": watch_expires_at,
        "history_id": history_id,
    }


@router.post("/email/{connector_id}/watch")
async def watch_gmail_connector(
    connector_id: str = Path(..., min_length=1),
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    connection = crud.get_oauth_connection(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        provider="google",
        system="gmail",
    )
    if connection is None or not connection.access_token:
        raise HTTPException(status_code=404, detail="Gmail OAuth connection not found")

    result = await GmailPubSubService(GmailAPIClient()).ensure_watch(
        db,
        connection=connection,
        connector_id=connector_id,
    )
    if result is None:
        raise HTTPException(status_code=400, detail="Gmail watch could not be started")

    updated = crud.get_oauth_connection(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        provider="google",
        system="gmail",
    )
    return {
        "success": True,
        "status": "watch_active",
        "connector_id": str(connector_id),
        "watch_active": True,
        "watch_expires_at": updated.gmail_watch_expiration if updated else None,
        "history_id": updated.gmail_history_id if updated else None,
        "email": updated.email if updated else connection.email,
    }


@router.post("/email/{connector_id}/unwatch")
async def unwatch_gmail_connector(
    connector_id: str = Path(..., min_length=1),
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    connection = crud.get_oauth_connection(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        provider="google",
        system="gmail",
    )
    if connection is None or not connection.access_token:
        raise HTTPException(status_code=404, detail="Gmail OAuth connection not found")

    await GmailPubSubService(GmailAPIClient()).stop_watch(db, connection=connection)
    return {
        "success": True,
        "status": "watch_disabled",
        "connector_id": str(connector_id),
        "watch_active": False,
        "email": connection.email,
    }


@router.post("/email/{connector_id}/execute")
async def execute_gmail_connector(
    connector_id: str = Path(..., min_length=1),
    payload: dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    connection = crud.get_oauth_connection(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        provider="google",
        system="gmail",
    )
    if connection is None or not connection.access_token:
        raise HTTPException(status_code=404, detail="Gmail OAuth connection not found")

    trigger_service = GmailTriggerService()
    result = await trigger_service.process_email_event(
        db,
        connection=connection,
        email_payload=payload,
        stream_key=str(connector_id),
    )
    return {
        "success": True,
        "connector_id": str(connector_id),
        "run": result.get("run"),
        "result": result.get("result"),
    }


@router.get("/email/{connector_id}/runs")
def list_gmail_connector_runs(
    connector_id: str = Path(..., min_length=1),
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    connection = crud.get_oauth_connection(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        provider="google",
        system="gmail",
    )
    target_connector_id = str(connector_id)
    if connection is not None and connection.gmail_connector_id:
        target_connector_id = str(connection.gmail_connector_id)

    logs = (
        db.query(EmailProcessingLog)
        .order_by(EmailProcessingLog.processed_at.desc())
        .limit(100)
        .all()
    )

    runs: list[dict[str, Any]] = []
    for log in logs:
        details = _parse_log_details(log.details)
        log_user_id = details.get("user_id")
        if log_user_id is not None and str(log_user_id) != str(AUTH_DISABLED_USER_ID):
            continue
        log_connector_id = str(details.get("connector_id") or "")
        if log_connector_id and log_connector_id != target_connector_id and log_connector_id != str(connector_id):
            continue
        if not log_connector_id and str(connector_id) != target_connector_id:
            continue

        runs.append(_serialize_run_from_log(log, details, connector_id=target_connector_id))

    return {"runs": runs[:50]}


@router.get("/email/{connector_id}/stream")
async def stream_gmail_connector(
    connector_id: str = Path(..., min_length=1),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> StreamingResponse:
    async def event_generator():
        async for payload in stream_gmail_connector_events(str(connector_id)):
            yield payload

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/automation-rules")
def list_automation_rules(
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    rules = get_automation_rules(db, connector="gmail", user_id=AUTH_DISABLED_USER_ID)
    latest_logs: dict[int, Any] = {}
    for log in get_automation_execution_logs(db, user_id=AUTH_DISABLED_USER_ID, connector_type="gmail", limit=200):
        if log.automation_rule_id not in latest_logs:
            latest_logs[log.automation_rule_id] = log
    return {
        "rules": [ _serialize_rule(rule, latest_logs.get(rule.id)) for rule in rules ],
    }


@router.get("/automation-logs")
def list_automation_logs(
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    logs = get_automation_execution_logs(db, user_id=AUTH_DISABLED_USER_ID, connector_type="gmail", limit=100)
    return {
        "logs": [_serialize_log(log) for log in logs],
    }


@router.patch("/automation-rules/{rule_id}")
def patch_automation_rule(
    rule_id: int = Path(..., ge=1),
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    rule = get_automation_rule_by_id(db, rule_id)
    # User ownership authorization check disabled.
    # if rule is None or rule.user_id != current_user.id:
    if rule is None:
        raise HTTPException(status_code=404, detail="Automation rule not found")

    is_active = payload.get("is_active") if isinstance(payload, dict) else None
    if is_active is None:
        raise HTTPException(status_code=400, detail="is_active is required")

    updated = update_automation_rule_status(db, rule_id=rule_id, is_active=bool(is_active))
    return {"success": True, "rule": _serialize_rule(updated)}


@router.post("/continue-missing-field")
def continue_missing_field(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, Any]:
    missing_field = str(payload.get("missing_field") or "").strip()
    user_answer = str(payload.get("user_answer") or "").strip()
    partial_intent = payload.get("partial_intent")

    if missing_field != "sender_email":
        raise HTTPException(status_code=400, detail="Unsupported missing field")
    if not partial_intent or not isinstance(partial_intent, dict):
        raise HTTPException(status_code=400, detail="partial_intent is required")
    if not _looks_like_email(user_answer):
        raise HTTPException(status_code=400, detail="user_answer must be a valid email address")

    filters = partial_intent.get("filters") if isinstance(partial_intent.get("filters"), dict) else {}
    sender_name = str(filters.get("sender_name") or filters.get("from") or "").strip() or None
    sender_email = user_answer.lower()
    filters["sender_email"] = sender_email
    filters["from"] = sender_email
    if sender_name:
        filters["sender_name"] = sender_name
        upsert_contact_mapping(
            db,
            user_id=AUTH_DISABLED_USER_ID,
            display_name=sender_name,
            email=sender_email,
            connector_type="gmail",
        )

    partial_intent["filters"] = filters
    partial_intent["execution_type"] = "automation_rule"
    partial_intent["connector"] = "gmail"
    partial_intent["trigger_type"] = partial_intent.get("trigger_type") or "new_email"

    intent = partial_intent
    rule = create_automation_rule(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        connector_type="gmail",
        trigger_type=str(intent.get("trigger_type") or "new_email"),
        sender_name=sender_name,
        sender_email=sender_email,
        subject_filter=str(filters.get("subject") or "") or None,
        keyword_filter=list(filters.get("keywords") or []),
        operation=str(intent.get("operation") or "analyse_and_reply"),
        tone=str((intent.get("output_requirement") or {}).get("tone") or "polite"),
        output_requirement=dict(intent.get("output_requirement") or {}),
        is_active=True,
        trigger_filters={
            "from": sender_email,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": filters.get("subject"),
            "keywords": list(filters.get("keywords") or []),
            "is_unread": False,
            "has_attachment": bool(filters.get("has_attachment")),
        },
        actions=[],
    )

    return {
        "status": "automation_rule_created",
        "message": f"Automation rule created successfully for emails from {sender_name or sender_email}.",
        "rule_id": rule.id,
    }


def _looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[\w\.-]+@[\w\.-]+\.\w+", value))


def _serialize_rule(rule, latest_log=None) -> dict[str, Any]:
    if rule is None:
        return {}
    return {
        "id": rule.id,
        "user_id": rule.user_id,
        "connector_type": getattr(rule, "connector_type", None) or getattr(rule, "connector", None),
        "trigger_type": rule.trigger_type,
        "sender_name": getattr(rule, "sender_name", None),
        "sender_email": getattr(rule, "sender_email", None),
        "subject_filter": getattr(rule, "subject_filter", None),
        "keyword_filter": getattr(rule, "keyword_filter", None) or [],
        "operation": getattr(rule, "operation", None),
        "tone": getattr(rule, "tone", None),
        "output_requirement": getattr(rule, "output_requirement", None) or {},
        "is_active": bool(rule.is_active),
        "last_processed_message_id": getattr(rule, "last_processed_message_id", None),
        "created_at": rule.created_at,
        "updated_at": getattr(rule, "updated_at", None),
        "last_execution_status": getattr(latest_log, "status", None),
        "last_execution_message_id": getattr(latest_log, "matched_message_id", None),
        "last_execution_action": getattr(latest_log, "action_taken", None),
    }


def _serialize_log(log) -> dict[str, Any]:
    return {
        "id": log.id,
        "automation_rule_id": log.automation_rule_id,
        "user_id": log.user_id,
        "connector_type": log.connector_type,
        "trigger_event_id": log.trigger_event_id,
        "matched_message_id": log.matched_message_id,
        "status": log.status,
        "action_taken": log.action_taken,
        "error_message": log.error_message,
        "created_at": log.created_at,
    }


def _parse_log_details(details: str | None) -> dict[str, Any]:
    if not details:
        return {}
    try:
        parsed = json.loads(details)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _serialize_run_from_log(log: EmailProcessingLog, details: dict[str, Any], *, connector_id: str) -> dict[str, Any]:
    input_payload = details.get("input_payload") if isinstance(details.get("input_payload"), dict) else {}
    agent_output = details.get("agent_output")
    if agent_output is None:
        results = details.get("results")
        if isinstance(results, list) and results:
            agent_output = results[0]

    sender = (
        input_payload.get("sender_email")
        or input_payload.get("sender")
        or input_payload.get("from")
        or "unknown sender"
    )
    return {
        "id": log.id,
        "connector_id": connector_id,
        "connector_type": details.get("connector_type") or "email",
        "status": str(details.get("status") or log.action_taken or "completed"),
        "trigger_summary": details.get("trigger_summary") or f"email from {sender}",
        "created_at": str(log.processed_at),
        "completed_at": str(log.processed_at),
        "duration_ms": details.get("duration_ms"),
        "input_payload": input_payload,
        "agent_output": agent_output if isinstance(agent_output, (dict, list, str)) else details,
        "reply_sent": details.get("reply_sent"),
        "raw": {
            "log": _serialize_log(log),
            "details": details,
        },
    }
