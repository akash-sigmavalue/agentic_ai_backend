from __future__ import annotations

from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from api.schemas.connector.automation_execution_log import AutomationExecutionLog
from api.schemas.connector.automation_rule import AutomationRule
from api.schemas.connector.contact_mapping import ContactMapping
from api.schemas.connector.processed_email import ProcessedEmail

# from app.models.automation_execution_log import AutomationExecutionLog
# from app.models.automation_rule import AutomationRule
# from app.models.contact_mapping import ContactMapping
# from app.models.processed_email import ProcessedEmail


def create_automation_rule(
    db: Session,
    *,
    user_id: int,
    connector_type: str = "gmail",
    trigger_type: str = "new_email",
    sender_name: str | None = None,
    sender_email: str | None = None,
    subject_filter: str | None = None,
    keyword_filter: list[str] | None = None,
    operation: str | None = None,
    tone: str | None = None,
    output_requirement: dict[str, Any] | None = None,
    is_active: bool = True,
    last_processed_message_id: str | None = None,
    trigger_filters: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> AutomationRule:
    filters = trigger_filters or {
        "from": sender_email or sender_name,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject_filter,
        "keywords": keyword_filter or [],
        "is_unread": False,
        "has_attachment": False,
    }
    rule = AutomationRule(
        user_id=user_id,
        connector=connector_type,
        connector_type=connector_type,
        trigger_type=trigger_type,
        trigger_filters=filters,
        actions=actions or [],
        sender_name=sender_name,
        sender_email=sender_email,
        subject_filter=subject_filter,
        keyword_filter=keyword_filter or [],
        operation=operation,
        tone=tone,
        output_requirement=output_requirement or {},
        is_active=is_active,
        last_processed_message_id=last_processed_message_id,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def get_active_automation_rules(
    db: Session,
    *,
    connector: str | None = None,
    user_id: int | None = None,
) -> list[AutomationRule]:
    query = db.query(AutomationRule).filter(AutomationRule.is_active.is_(True))
    if connector:
        query = query.filter((AutomationRule.connector_type == connector) | (AutomationRule.connector == connector))
    if user_id is not None:
        query = query.filter(AutomationRule.user_id == user_id)
    return query.order_by(AutomationRule.created_at.asc()).all()


def get_automation_rules(
    db: Session,
    *,
    connector: str | None = None,
    user_id: int | None = None,
) -> list[AutomationRule]:
    query = db.query(AutomationRule)
    if connector:
        query = query.filter((AutomationRule.connector_type == connector) | (AutomationRule.connector == connector))
    if user_id is not None:
        query = query.filter(AutomationRule.user_id == user_id)
    return query.order_by(AutomationRule.created_at.asc()).all()


def get_automation_rule_by_id(db: Session, rule_id: int) -> AutomationRule | None:
    return db.query(AutomationRule).filter(AutomationRule.id == rule_id).first()


def update_automation_rule_status(db: Session, *, rule_id: int, is_active: bool) -> AutomationRule | None:
    rule = get_automation_rule_by_id(db, rule_id)
    if rule is None:
        return None
    rule.is_active = is_active
    db.commit()
    db.refresh(rule)
    return rule


def upsert_contact_mapping(
    db: Session,
    *,
    user_id: int,
    display_name: str,
    email: str,
    connector_type: str = "gmail",
) -> ContactMapping:
    normalized_name = display_name.strip()
    normalized_email = email.strip().lower()
    existing = (
        db.query(ContactMapping)
        .filter(
            ContactMapping.user_id == user_id,
            ContactMapping.display_name == normalized_name,
            ContactMapping.connector_type == connector_type,
        )
        .first()
    )
    if existing is None:
        existing = (
            db.query(ContactMapping)
            .filter(
                ContactMapping.user_id == user_id,
                ContactMapping.email == normalized_email,
                ContactMapping.connector_type == connector_type,
            )
            .first()
        )

    if existing is None:
        mapping = ContactMapping(
            user_id=user_id,
            display_name=normalized_name,
            email=normalized_email,
            connector_type=connector_type,
        )
        db.add(mapping)
        db.commit()
        db.refresh(mapping)
        return mapping

    existing.display_name = normalized_name
    existing.email = normalized_email
    existing.connector_type = connector_type
    db.commit()
    db.refresh(existing)
    return existing


def find_contact_mapping_by_name(
    db: Session,
    *,
    user_id: int,
    display_name: str,
    connector_type: str = "gmail",
) -> ContactMapping | None:
    normalized_name = display_name.strip()
    if not normalized_name:
        return None
    return (
        db.query(ContactMapping)
        .filter(
            ContactMapping.user_id == user_id,
            ContactMapping.display_name.ilike(normalized_name),
            ContactMapping.connector_type == connector_type,
        )
        .first()
    )


def find_contact_mapping_by_email(
    db: Session,
    *,
    user_id: int,
    email: str,
    connector_type: str = "gmail",
) -> ContactMapping | None:
    normalized_email = email.strip().lower()
    if not normalized_email:
        return None
    return (
        db.query(ContactMapping)
        .filter(
            ContactMapping.user_id == user_id,
            ContactMapping.email == normalized_email,
            ContactMapping.connector_type == connector_type,
        )
        .first()
    )


def create_automation_execution_log(
    db: Session,
    *,
    automation_rule_id: int,
    user_id: int,
    connector_type: str,
    trigger_event_id: str | None,
    matched_message_id: str,
    status: str,
    action_taken: str,
    error_message: str | None = None,
) -> AutomationExecutionLog:
    log_entry = AutomationExecutionLog(
        automation_rule_id=automation_rule_id,
        user_id=user_id,
        connector_type=connector_type,
        trigger_event_id=trigger_event_id,
        matched_message_id=matched_message_id,
        status=status,
        action_taken=action_taken,
        error_message=error_message,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)
    return log_entry


def has_successful_execution_for_message(db: Session, *, message_id: str) -> bool:
    if not message_id:
        return False
    return (
        db.query(AutomationExecutionLog)
        .filter(
            AutomationExecutionLog.matched_message_id == message_id,
            AutomationExecutionLog.status == "success",
        )
        .first()
        is not None
    )


def get_automation_execution_logs(
    db: Session,
    *,
    user_id: int | None = None,
    connector_type: str | None = None,
    limit: int = 50,
) -> list[AutomationExecutionLog]:
    query = db.query(AutomationExecutionLog)
    if user_id is not None:
        query = query.filter(AutomationExecutionLog.user_id == user_id)
    if connector_type:
        query = query.filter(AutomationExecutionLog.connector_type == connector_type)
    return query.order_by(desc(AutomationExecutionLog.created_at)).limit(limit).all()


def is_email_processed(db: Session, rule_id: int, thread_id: str) -> bool:
    return (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.automation_rule_id == rule_id,
            ProcessedEmail.thread_id == thread_id,
        )
        .first()
        is not None
    )


def mark_email_processed(db: Session, rule_id: int, thread_id: str, message_id: str | None = None) -> ProcessedEmail:
    existing = (
        db.query(ProcessedEmail)
        .filter(
            ProcessedEmail.automation_rule_id == rule_id,
            ProcessedEmail.thread_id == thread_id,
        )
        .first()
    )
    if existing is not None:
        if message_id and not existing.message_id:
            existing.message_id = message_id
            db.commit()
            db.refresh(existing)
        return existing

    processed = ProcessedEmail(
        automation_rule_id=rule_id,
        thread_id=thread_id,
        message_id=message_id,
    )
    db.add(processed)
    db.commit()
    db.refresh(processed)
    return processed
