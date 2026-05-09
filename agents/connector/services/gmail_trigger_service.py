from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

# from app.agents.connector_agent import ConnectorAgent
from agents.connector.connector_agent import ConnectorAgent
from agents.connector.workflows.automation_executor import AutomationExecutor
from database.connector import crud
from agents.connector.services.gmail_connector_streams import gmail_connector_event_hub
# from app.workflows.automation_executor import AutomationExecutor


class GmailTriggerService:
    def __init__(
        self,
        connector_agent: ConnectorAgent | None = None,
        automation_executor: AutomationExecutor | None = None,
    ) -> None:
        self._connector_agent = connector_agent or ConnectorAgent()
        self._automation_executor = automation_executor or AutomationExecutor(self._connector_agent)

    def _resolve_connector_id(self, connection, fallback: str | None = None) -> str:
        connector_id = str(getattr(connection, "gmail_connector_id", None) or "").strip()
        if connector_id:
            return connector_id
        if fallback:
            return fallback
        return str(getattr(connection, "id", ""))

    def _build_run_snapshot(
        self,
        *,
        connection,
        email_payload: dict[str, Any],
        result: dict[str, Any],
        connector_id: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        sender = (
            email_payload.get("sender_email")
            or email_payload.get("sender")
            or email_payload.get("from")
            or "unknown sender"
        )
        message_id = str(
            email_payload.get("message_id")
            or email_payload.get("messageId")
            or result.get("message_id")
            or email_payload.get("history_id")
            or email_payload.get("historyId")
            or now
        )
        status = str(result.get("status") or "processed")
        action_taken = str(result.get("action_taken") or "processed")
        reply_sent = bool(
            result.get("reply")
            or action_taken.startswith("reply")
            or action_taken.startswith("draft")
        )

        return {
            "id": message_id,
            "connector_id": connector_id,
            "connector_type": "email",
            "status": status,
            "trigger_summary": f"email from {sender}",
            "created_at": now,
            "completed_at": now,
            "duration_ms": 0,
            "input_payload": email_payload,
            "agent_output": result,
            "reply_sent": reply_sent,
            "raw": {
                "user_id": connection.user_id,
                "connector_id": connector_id,
                "result": result,
            },
        }

    async def process_email_event(
        self,
        db: Session,
        *,
        connection,
        email_payload: dict[str, Any],
        stream_key: str | None = None,
    ) -> dict[str, Any]:
        connector_id = self._resolve_connector_id(connection, fallback=stream_key)
        start_event = {
            "event_type": "stage_start",
            "node": "gmail_trigger",
            "connector_id": connector_id,
            "message": "Processing Gmail trigger",
            "data": email_payload,
        }
        await gmail_connector_event_hub.publish(connector_id, start_event)

        result = await self._automation_executor.process_incoming_email(
            db,
            user_id=connection.user_id,
            email_payload=email_payload,
        )

        run_snapshot = self._build_run_snapshot(
            connection=connection,
            email_payload=email_payload,
            result=result,
            connector_id=connector_id,
        )

        await gmail_connector_event_hub.publish(
            connector_id,
            {
                "event_type": "stage_complete",
                "node": "gmail_trigger",
                "connector_id": connector_id,
                "message": "Gmail trigger processed",
                "data": {"result": result},
                "run": run_snapshot,
            },
        )
        await gmail_connector_event_hub.publish(
            connector_id,
            {
                "event_type": "final_result",
                "node": "output",
                "connector_id": connector_id,
                "message": "Final run result",
                "data": result,
                "run": run_snapshot,
            },
        )

        return {
            "success": True,
            "connector_id": connector_id,
            "run": run_snapshot,
            "result": result,
        }
