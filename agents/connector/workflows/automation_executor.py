from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from agents.connector.connector_agent import ConnectorAgent
from agents.connector.services.gmail_workflow_ai import GmailWorkflowAI
from api.schemas.connector.request_models import ConnectorTaskRequest
from database.connector.automation_crud import (
    create_automation_execution_log,
    get_active_automation_rules,
    has_successful_execution_for_message,
    mark_email_processed,
    upsert_contact_mapping,
)

from database.connector import crud
from utils.connector.gmail_client import GmailAPIClient, GmailAPIError


logger = logging.getLogger(__name__)


class AutomationExecutor:
    """Executes stored Gmail automation rules against Gmail webhook or polling events."""

    def __init__(
        self,
        connector_agent: ConnectorAgent,
        gmail_ai: GmailWorkflowAI | None = None,
        gmail_client: GmailAPIClient | None = None,
    ) -> None:
        self._connector_agent = connector_agent
        self._gmail_ai = gmail_ai or GmailWorkflowAI()
        self._gmail_client = gmail_client or GmailAPIClient()

    async def process_incoming_email(
        self,
        db: Session,
        *,
        user_id: int,
        email_payload: dict[str, Any],
        current_user=None,
        trace=None,
    ) -> dict[str, Any]:
        normalized_email = self._normalize_incoming_email(email_payload)
        message_id = str(normalized_email.get("message_id") or "").strip()
        thread_id = str(normalized_email.get("thread_id") or "").strip()

        if not message_id:
            crud.create_email_processing_log(
                db,
                message_id=str(normalized_email.get("history_id") or "missing-message-id"),
                thread_id=str(normalized_email.get("thread_id") or "missing-message-id"),
                action_taken="ignored",
                details=json.dumps(
                    {
                        "user_id": user_id,
                        "connector_type": "email",
                        "input_payload": normalized_email,
                        "status": "ignored",
                        "reason": "Missing message_id in Gmail event payload",
                    },
                    default=str,
                ),
            )
            return {
                "success": False,
                "status": "ignored",
                "message": "Missing message_id in Gmail event payload",
                "matched_rules": [],
                "results": [],
            }

        rules = self._load_matching_rules(db, user_id=user_id, email=normalized_email)
        if not rules:
            crud.create_email_processing_log(
                db,
                message_id=message_id,
                thread_id=thread_id or message_id,
                action_taken="no_matching_rules",
                details=json.dumps(
                    {
                        "user_id": user_id,
                        "connector_type": "email",
                        "connector_id": getattr(
                            crud.get_oauth_connection(db, user_id=user_id, provider="google", system="gmail"),
                            "gmail_connector_id",
                            None,
                        ),
                        "input_payload": normalized_email,
                        "status": "no_matching_rules",
                        "matched_rules": [],
                        "results": [],
                    },
                    default=str,
                ),
            )
            return {
                "success": True,
                "status": "no_matching_rules",
                "message_id": message_id,
                "thread_id": thread_id,
                "matched_rules": [],
                "results": [],
            }

        if has_successful_execution_for_message(db, message_id=message_id):
            results: list[dict[str, Any]] = []
            for rule in rules:
                results.append(
                    self._log_duplicate_skip(
                        db,
                        rule=rule,
                        user_id=user_id,
                        message_id=message_id,
                        thread_id=thread_id,
                        trigger_event_id=str(normalized_email.get("history_id") or message_id),
                    )
                )
            crud.create_email_processing_log(
                db,
                message_id=message_id,
                thread_id=thread_id or message_id,
                action_taken="duplicate_skipped",
                details=json.dumps(
                    {
                        "user_id": user_id,
                        "connector_type": "email",
                        "input_payload": normalized_email,
                        "status": "duplicate_skipped",
                        "matched_rules": [rule.id for rule in rules],
                        "results": results,
                    },
                    default=str,
                ),
            )
            return {
                "success": True,
                "status": "duplicate_skipped",
                "message_id": message_id,
                "thread_id": thread_id,
                "matched_rules": [rule.id for rule in rules],
                "results": results,
            }

        results: list[dict[str, Any]] = []
        for rule in rules:
            result = await self.execute_gmail_rule(
                db,
                user_id=user_id,
                rule=rule,
                message_id=message_id,
                trigger_event_id=str(normalized_email.get("history_id") or message_id),
                email_context=normalized_email,
                current_user=current_user,
                trace=trace,
            )
            results.append(result)

        connection = crud.get_oauth_connection(
            db,
            user_id=user_id,
            provider="google",
            system="gmail",
        )
        connector_id = str(getattr(connection, "gmail_connector_id", None) or getattr(connection, "id", "") or "")
        primary_result = next((item for item in results if item.get("status") == "success"), results[0] if results else {})
        details_payload = {
            "user_id": user_id,
            "connector_id": connector_id or None,
            "connector_type": "email",
            "message_id": message_id,
            "thread_id": thread_id or message_id,
            "input_payload": normalized_email,
            "matched_rules": [rule.id for rule in rules],
            "results": results,
            "status": "processed" if any(result.get("status") == "success" for result in results) else "skipped",
            "agent_output": primary_result,
            "reply_sent": any(
                bool(result.get("reply"))
                or str(result.get("action_taken") or "").startswith("reply")
                for result in results
            ),
        }
        crud.create_email_processing_log(
            db,
            message_id=message_id,
            thread_id=thread_id or message_id,
            action_taken=str(primary_result.get("action_taken") or "processed"),
            details=json.dumps(details_payload, default=str),
        )

        return {
            "success": True,
            "status": "processed" if any(result.get("status") == "success" for result in results) else "skipped",
            "message_id": message_id,
            "thread_id": thread_id,
            "matched_rules": [rule.id for rule in rules],
            "results": results,
        }

    async def execute_gmail_rule(
        self,
        db: Session,
        *,
        user_id: int,
        rule,
        message_id: str,
        trigger_event_id: str | None = None,
        email_context: dict[str, Any] | None = None,
        current_user=None,
        trace=None,
    ) -> dict[str, Any]:
        connector_type = str(getattr(rule, "connector_type", None) or getattr(rule, "connector", None) or "gmail")
        sender_name = getattr(rule, "sender_name", None)
        sender_email = getattr(rule, "sender_email", None)

        if has_successful_execution_for_message(db, message_id=message_id):
            return self._log_duplicate_skip(
                db,
                rule=rule,
                user_id=user_id,
                message_id=message_id,
                thread_id=str((email_context or {}).get("thread_id") or ""),
                trigger_event_id=trigger_event_id,
            )

        connection = crud.get_oauth_connection(
            db,
            user_id=user_id,
            provider="google",
            system="gmail",
        )
        if connection is None or not connection.access_token:
            return self._create_failure_log(
                db,
                rule=rule,
                user_id=user_id,
                message_id=message_id,
                trigger_event_id=trigger_event_id,
                action_taken="oauth_missing",
                error_message="Gmail OAuth connection missing",
            )

        try:
            message = await self._gmail_client.read_message(connection.access_token, message_id, trace=trace)
        except GmailAPIError as exc:
            return self._create_failure_log(
                db,
                rule=rule,
                user_id=user_id,
                message_id=message_id,
                trigger_event_id=trigger_event_id,
                action_taken="read_failed",
                error_message=str(exc),
            )

        normalized_message = self._normalize_gmail_message(message)
        normalized_message["message_id"] = message_id
        normalized_message["thread_id"] = normalized_message.get("thread_id") or (email_context or {}).get("thread_id")

        if not self._rule_matches_email(rule, normalized_message):
            return self._create_skip_log(
                db,
                rule=rule,
                user_id=user_id,
                message_id=message_id,
                trigger_event_id=trigger_event_id,
                action_taken="rule_not_matched",
                status="skipped",
                error_message="Incoming message did not match the automation rule filters",
            )

        if self._should_skip_message(normalized_message):
            return self._create_skip_log(
                db,
                rule=rule,
                user_id=user_id,
                message_id=message_id,
                trigger_event_id=trigger_event_id,
                action_taken="automated_email_skipped",
                status="skipped",
                error_message="Skipped automated or no-reply sender",
            )

        content = {
            "thread_id": normalized_message.get("thread_id"),
            "message_id": normalized_message.get("message_id"),
            "from_email": normalized_message.get("sender_email"),
            "sender_name": normalized_message.get("sender_name"),
            "subject": normalized_message.get("subject"),
            "body": normalized_message.get("body"),
            "snippet": normalized_message.get("snippet"),
            "raw": normalized_message.get("raw"),
        }

        output_requirement = self._as_dict(getattr(rule, "output_requirement", None))
        tone = str(getattr(rule, "tone", None) or output_requirement.get("tone") or "professional")

        analysis_text: str | None = None
        reply_text: str | None = None
        reply_result: dict[str, Any] | None = None
        action_taken = "skipped"

        try:
            if bool(output_requirement.get("summary")):
                analysis_text = self._normalize_llm_text(self._gmail_ai.summarize(content, instruction="Summarize this incoming email").get("summary"))
            elif bool(output_requirement.get("analysis")):
                analysis_text = self._normalize_llm_text(self._gmail_ai.analyze(content, instruction="Analyze this incoming email").get("analysis"))

            if bool(output_requirement.get("reply_required")):
                reply_result = self._gmail_ai.generate_reply(
                    content,
                    instruction=self._build_reply_instruction(rule, content, analysis_text),
                    tone=tone,
                )
                reply_text = self._normalize_llm_text(reply_result.get("reply"))

            if bool(output_requirement.get("reply_required")) and not reply_text:
                return self._create_skip_log(
                    db,
                    rule=rule,
                    user_id=user_id,
                    message_id=message_id,
                    trigger_event_id=trigger_event_id,
                    action_taken="empty_reply_skipped",
                    status="skipped",
                    error_message="LLM returned an empty reply",
                )

            if bool(output_requirement.get("reply_required")):
                if bool(output_requirement.get("draft_only")):
                    send_result = await self._call_gmail_connector(
                        db,
                        user_id=user_id,
                        operation="gmail.draft_email",
                        payload={
                            "to": normalized_message.get("sender_email") or normalized_message.get("sender"),
                            "subject": self._reply_subject(normalized_message.get("subject")),
                            "body": reply_text or "",
                            "thread_id": normalized_message.get("thread_id"),
                        },
                        current_user=current_user,
                        trace=trace,
                    )
                    action_taken = "draft_created" if send_result.get("ok", True) else "draft_failed"
                elif bool(output_requirement.get("send_directly")):
                    send_result = await self._call_gmail_connector(
                        db,
                        user_id=user_id,
                        operation="gmail.reply_to_thread",
                        payload={
                            "thread_id": normalized_message.get("thread_id") or "",
                            "to": normalized_message.get("sender"),
                            "subject": self._reply_subject(normalized_message.get("subject")),
                            "body": reply_text or "",
                        },
                        current_user=current_user,
                        trace=trace,
                    )
                    action_taken = "reply_sent" if send_result.get("ok", True) else "reply_failed"
                else:
                    action_taken = "reply_prepared"
            elif analysis_text:
                action_taken = "analysis_completed"
            else:
                action_taken = "skipped"

            status = "success"
            if bool(output_requirement.get("reply_required")) and action_taken in {"draft_failed", "reply_failed"}:
                status = "failed"
            if bool(output_requirement.get("reply_required")) and not reply_text:
                status = "skipped"

            log_entry = create_automation_execution_log(
                db,
                automation_rule_id=rule.id,
                user_id=user_id,
                connector_type=connector_type,
                trigger_event_id=trigger_event_id,
                matched_message_id=message_id,
                status=status,
                action_taken=action_taken,
                error_message=None if status == "success" else "Automation execution did not complete successfully",
            )

            if status == "success":
                self._mark_rule_processed(
                    db,
                    rule=rule,
                    message_id=message_id,
                    thread_id=str(normalized_message.get("thread_id") or ""),
                )
                if sender_name and normalized_message.get("sender_email"):
                    upsert_contact_mapping(
                        db,
                        user_id=user_id,
                        display_name=sender_name,
                        email=str(normalized_message.get("sender_email")),
                        connector_type=connector_type,
                    )

            return {
                "success": status == "success",
                "status": status,
                "rule_id": rule.id,
                "message_id": message_id,
                "action_taken": action_taken,
                "analysis": analysis_text,
                "reply": reply_text,
                "log_id": log_entry.id,
            }
        except GmailAPIError as exc:
            return self._create_failure_log(
                db,
                rule=rule,
                user_id=user_id,
                message_id=message_id,
                trigger_event_id=trigger_event_id,
                action_taken="gmail_api_failed",
                error_message=str(exc),
            )
        except Exception as exc:
            logger.exception("automation execution failed for rule %s", getattr(rule, "id", "-"))
            return self._create_failure_log(
                db,
                rule=rule,
                user_id=user_id,
                message_id=message_id,
                trigger_event_id=trigger_event_id,
                action_taken="automation_failed",
                error_message=str(exc),
            )

    async def _call_gmail_connector(
        self,
        db: Session,
        *,
        user_id: int,
        operation: str,
        payload: dict[str, Any],
        current_user=None,
        trace=None,
    ) -> dict[str, Any]:
        task = ConnectorTaskRequest(system="gmail", operation=operation, input=payload)
        result = await self._connector_agent.execute(
            task,
            db=db,
            user_id=user_id if user_id is not None else getattr(current_user, "id", None),
            trace=trace,
        )
        return result if isinstance(result, dict) else {"ok": True, "data": result}

    def _load_matching_rules(self, db: Session, *, user_id: int, email: dict[str, Any]) -> list[Any]:
        candidates = get_active_automation_rules(db, connector="gmail", user_id=user_id)
        return [rule for rule in candidates if self._rule_matches_email(rule, email)]

    def _rule_matches_email(self, rule, email: dict[str, Any]) -> bool:
        rule_filters = self._as_dict(getattr(rule, "trigger_filters", None))
        sender_email = self._normalize_email(rule_filters.get("sender_email") or getattr(rule, "sender_email", None) or rule_filters.get("from"))
        sender_name = self._normalize_text(rule_filters.get("sender_name") or getattr(rule, "sender_name", None))
        subject_filter = self._normalize_text(rule_filters.get("subject") or getattr(rule, "subject_filter", None))
        keywords = rule_filters.get("keywords") or getattr(rule, "keyword_filter", None) or []

        message_sender_email = self._normalize_email(email.get("sender_email"))
        message_sender_name = self._normalize_text(email.get("sender_name"))
        message_subject = self._normalize_text(email.get("subject"))
        message_body = self._normalize_text(email.get("body"))

        if sender_email and sender_email != message_sender_email:
            return False

        if sender_name and sender_name not in message_sender_name and sender_name not in self._normalize_text(email.get("sender") or ""):
            return False

        if subject_filter and subject_filter not in message_subject:
            return False

        if keywords:
            searchable = f"{message_subject} {message_body}"
            if not any(self._normalize_text(str(keyword)) in searchable for keyword in keywords if str(keyword).strip()):
                return False

        return True

    def _should_skip_message(self, email: dict[str, Any]) -> bool:
        sender_email = self._normalize_email(email.get("sender_email") or email.get("sender"))
        sender_text = self._normalize_text(email.get("sender") or email.get("sender_name") or sender_email or "")
        subject = self._normalize_text(email.get("subject"))
        headers = self._as_dict(email.get("raw")).get("payload", {}).get("headers", []) if isinstance(self._as_dict(email.get("raw")).get("payload"), dict) else []

        skip_tokens = (
            "noreply",
            "no-reply",
            "do-not-reply",
            "donotreply",
            "mailer-daemon",
            "automated",
            "system",
            "notification",
        )
        if sender_email and any(token in sender_email for token in skip_tokens):
            return True
        if any(token in sender_text for token in skip_tokens):
            return True
        if any(token in subject for token in ("auto", "system generated", "do not reply")):
            return True

        for header in headers if isinstance(headers, list) else []:
            if not isinstance(header, dict):
                continue
            header_name = self._normalize_text(header.get("name") or "")
            header_value = self._normalize_text(header.get("value") or "")
            if header_name in {"auto-submitted", "precedence", "x-auto-response-suppress"} and header_value:
                return True
        return False

    def _normalize_incoming_email(self, payload: dict[str, Any]) -> dict[str, Any]:
        email = payload.get("email") if isinstance(payload.get("email"), dict) else payload
        thread_id = str((email or {}).get("thread_id") or (email or {}).get("threadId") or payload.get("thread_id") or "").strip() or None
        message_id = str((email or {}).get("message_id") or (email or {}).get("messageId") or payload.get("message_id") or "").strip() or None
        sender_raw = self._extract_sender(email)
        sender_email = self._extract_email(sender_raw) or self._extract_email(payload.get("sender_email") or payload.get("sender"))
        sender_name = self._extract_sender_name(sender_raw)
        subject = self._extract_subject(email)
        body = self._extract_body(email)

        return {
            "sender": sender_raw,
            "sender_email": sender_email,
            "sender_name": sender_name,
            "subject": subject,
            "body": body,
            "snippet": payload.get("snippet") or (email or {}).get("snippet"),
            "thread_id": thread_id,
            "message_id": message_id,
            "has_attachment": bool(payload.get("has_attachment") or self._has_attachment(email)),
            "is_unread": bool(payload.get("is_unread", True)),
            "history_id": str(payload.get("history_id") or payload.get("historyId") or "").strip() or None,
            "received_at": payload.get("received_at") or payload.get("timestamp"),
            "raw": payload,
        }

    def _normalize_gmail_message(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        headers = payload.get("headers") if isinstance(payload, dict) and isinstance(payload.get("headers"), list) else []
        sender_raw = self._find_header(headers, "From")
        subject = self._find_header(headers, "Subject") or message.get("subject")
        sender_email = self._extract_email(sender_raw)
        sender_name = self._extract_sender_name(sender_raw)
        body = self._extract_message_body(message) or message.get("snippet")

        return {
            "sender": sender_raw,
            "sender_email": sender_email,
            "sender_name": sender_name,
            "subject": subject,
            "body": body,
            "snippet": message.get("snippet"),
            "thread_id": message.get("threadId") or message.get("thread_id"),
            "message_id": message.get("id") or message.get("message_id"),
            "has_attachment": self._has_attachment(message),
            "is_unread": True,
            "raw": message,
        }

    def _extract_sender(self, email: Any) -> str | None:
        if not isinstance(email, dict):
            return None
        for key in ("sender", "from", "from_email", "emailAddress"):
            value = email.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _extract_sender_name(self, sender: str | None) -> str | None:
        if not sender:
            return None
        if "<" in sender and ">" in sender:
            return sender.split("<", 1)[0].strip().strip('"') or None
        return sender if "@" not in sender else None

    def _extract_email(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", value)
        if match:
            return match.group(0).strip().lower()
        if self._normalize_text(value) in {"noreply", "no reply"}:
            return self._normalize_text(value)
        return None

    def _extract_subject(self, email: Any) -> str | None:
        if not isinstance(email, dict):
            return None
        for key in ("subject", "Subject"):
            value = email.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        headers = self._as_dict(email).get("payload", {}).get("headers", [])
        return self._find_header(headers, "Subject")

    def _extract_body(self, email: Any) -> str | None:
        if not isinstance(email, dict):
            return None
        for key in ("body", "snippet"):
            value = email.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return self._extract_message_body(email)

    def _extract_message_body(self, message: dict[str, Any]) -> str | None:
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

    def _has_attachment(self, message: dict[str, Any] | None) -> bool:
        if not isinstance(message, dict):
            return False
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return False
        parts = payload.get("parts")
        if not isinstance(parts, list):
            return False
        return any(isinstance(part, dict) and part.get("filename") for part in parts)

    def _find_header(self, headers: Any, header_name: str) -> str | None:
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

    def _build_reply_instruction(self, rule, content: dict[str, Any], analysis_text: str | None) -> str:
        parts = [
            "Draft a helpful reply to this email.",
            f"Tone: {getattr(rule, 'tone', None) or 'professional'}.",
        ]
        if analysis_text:
            parts.append(f"Use this analysis: {analysis_text}")
        return " ".join(parts)

    def _reply_subject(self, subject: str | None) -> str:
        subject_text = subject or "Email"
        if subject_text.lower().startswith("re:"):
            return subject_text
        return f"Re: {subject_text}"

    def _normalize_llm_text(self, value: Any) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return None

    def _as_dict(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return {}

    def _normalize_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    def _normalize_email(self, value: Any) -> str:
        email = self._extract_email(value)
        return email or self._normalize_text(value)

    def _mark_rule_processed(self, db: Session, *, rule, message_id: str, thread_id: str | None = None) -> None:
        if getattr(rule, "last_processed_message_id", None) == message_id:
            return
        rule.last_processed_message_id = message_id
        db.commit()
        db.refresh(rule)
        try:
            mark_email_processed(db, rule.id, str(thread_id or message_id), message_id)
        except Exception:
            db.rollback()

    def _create_skip_log(
        self,
        db: Session,
        *,
        rule,
        user_id: int,
        message_id: str,
        trigger_event_id: str | None,
        action_taken: str,
        status: str,
        error_message: str | None,
    ) -> dict[str, Any]:
        log_entry = create_automation_execution_log(
            db,
            automation_rule_id=rule.id,
            user_id=user_id,
            connector_type=str(getattr(rule, "connector_type", None) or getattr(rule, "connector", None) or "gmail"),
            trigger_event_id=trigger_event_id,
            matched_message_id=message_id,
            status=status,
            action_taken=action_taken,
            error_message=error_message,
        )
        return {
            "success": False,
            "status": status,
            "rule_id": rule.id,
            "message_id": message_id,
            "action_taken": action_taken,
            "error_message": error_message,
            "log_id": log_entry.id,
        }

    def _create_failure_log(
        self,
        db: Session,
        *,
        rule,
        user_id: int,
        message_id: str,
        trigger_event_id: str | None,
        action_taken: str,
        error_message: str,
    ) -> dict[str, Any]:
        return self._create_skip_log(
            db,
            rule=rule,
            user_id=user_id,
            message_id=message_id,
            trigger_event_id=trigger_event_id,
            action_taken=action_taken,
            status="failed",
            error_message=error_message,
        )

    def _log_duplicate_skip(
        self,
        db: Session,
        *,
        rule,
        user_id: int,
        message_id: str,
        thread_id: str,
        trigger_event_id: str | None,
    ) -> dict[str, Any]:
        return self._create_skip_log(
            db,
            rule=rule,
            user_id=user_id,
            message_id=message_id,
            trigger_event_id=trigger_event_id,
            action_taken="duplicate_skipped",
            status="skipped",
            error_message=f"Message {message_id} already processed",
        )
