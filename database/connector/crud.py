from datetime import datetime

from sqlalchemy.orm import Session


# from app.models.email_processing_log import EmailProcessingLog
from api.schemas.connector.email_processing_log import EmailProcessingLog
from api.schemas.connector.contact_mapping import ContactMapping
from api.schemas.connector.sender_preference import SenderPreference
from api.schemas.connector import user as user_schema
# from app.auth.security import get_password_hash
from auth.security import get_password_hash
from database.connector import models

def get_user_by_username(db: Session, username: str):
    return db.query(models.User).filter(models.User.username == username).first()

def create_user(db: Session, user: user_schema.UserCreate):
    hashed_password = get_password_hash(user.password)
    db_user = models.User(username=user.username, hashed_password=hashed_password)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def get_oauth_connection(db: Session, user_id: int, provider: str, system: str):
    return (
        db.query(models.OAuthConnection)
        .filter(
            models.OAuthConnection.user_id == user_id,
            models.OAuthConnection.provider == provider,
            models.OAuthConnection.system == system,
        )
        .first()
    )


def get_oauth_connection_by_email(db: Session, *, provider: str, system: str, email: str):
    return (
        db.query(models.OAuthConnection)
        .filter(
            models.OAuthConnection.provider == provider,
            models.OAuthConnection.system == system,
            models.OAuthConnection.email == email,
        )
        .first()
    )


def get_oauth_connections(db: Session, *, provider: str | None = None, system: str | None = None):
    query = db.query(models.OAuthConnection)
    if provider:
        query = query.filter(models.OAuthConnection.provider == provider)
    if system:
        query = query.filter(models.OAuthConnection.system == system)
    return query.all()


def find_contact_mapping_by_name(db: Session, name: str, current_user):
    normalized_name = str(name or "").strip()
    user_id = getattr(current_user, "id", None)
    if not normalized_name or user_id is None:
        return None

    return (
        db.query(ContactMapping)
        .filter(
            ContactMapping.user_id == user_id,
            ContactMapping.display_name.ilike(normalized_name),
        )
        .first()
    )


def upsert_oauth_connection(
    db: Session,
    *,
    user_id: int,
    provider: str,
    system: str,
    email: str | None = None,
    access_token: str,
    refresh_token: str | None = None,
    token_type: str | None = None,
    expires_at: datetime | None = None,
    scope: str | None = None,
):
    db_connection = get_oauth_connection(db, user_id=user_id, provider=provider, system=system)
    if db_connection is None:
        db_connection = models.OAuthConnection(
            user_id=user_id,
            provider=provider,
            system=system,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            scope=scope,
        )
        db.add(db_connection)
    else:
        db_connection.email = email or db_connection.email
        db_connection.access_token = access_token
        if refresh_token:
            db_connection.refresh_token = refresh_token
        if token_type is not None:
            db_connection.token_type = token_type
        db_connection.expires_at = expires_at
        if scope is not None:
            db_connection.scope = scope

    db.commit()
    db.refresh(db_connection)
    return db_connection


def update_oauth_watch_state(
    db: Session,
    *,
    user_id: int,
    provider: str,
    system: str,
    gmail_connector_id: str | None = None,
    gmail_history_id: str | None = None,
    gmail_watch_expiration: datetime | None = None,
):
    connection = get_oauth_connection(db, user_id=user_id, provider=provider, system=system)
    if connection is None:
        return None

    if gmail_connector_id is not None:
        connection.gmail_connector_id = gmail_connector_id
    if gmail_history_id is not None:
        connection.gmail_history_id = gmail_history_id
    if gmail_watch_expiration is not None:
        connection.gmail_watch_expiration = gmail_watch_expiration
    db.commit()
    db.refresh(connection)
    return connection


def clear_oauth_watch_state(
    db: Session,
    *,
    user_id: int,
    provider: str,
    system: str,
):
    connection = get_oauth_connection(db, user_id=user_id, provider=provider, system=system)
    if connection is None:
        return None

    connection.gmail_connector_id = None
    connection.gmail_history_id = None
    connection.gmail_watch_expiration = None
    db.commit()
    db.refresh(connection)
    return connection


def get_sender_preference(db: Session, *, user_id: int, sender_email: str) -> SenderPreference | None:
    return (
        db.query(SenderPreference)
        .filter(
            SenderPreference.user_id == user_id,
            SenderPreference.sender_email == sender_email,
        )
        .first()
    )


def upsert_sender_preference(
    db: Session,
    *,
    user_id: int,
    sender_email: str,
    tone: str | None = None,
    auto_send_allowed: bool = False,
    trust_level: float = 0.0,
) -> SenderPreference:
    preference = get_sender_preference(db, user_id=user_id, sender_email=sender_email)
    if preference is None:
        preference = SenderPreference(
            user_id=user_id,
            sender_email=sender_email,
            tone=tone,
            auto_send_allowed=auto_send_allowed,
            trust_level=trust_level,
        )
        db.add(preference)
    else:
        if tone is not None:
            preference.tone = tone
        preference.auto_send_allowed = auto_send_allowed
        preference.trust_level = trust_level
    db.commit()
    db.refresh(preference)
    return preference


def has_email_processing_log(db: Session, *, message_id: str) -> bool:
    return db.query(EmailProcessingLog).filter(EmailProcessingLog.message_id == message_id).first() is not None


def create_email_processing_log(
    db: Session,
    *,
    message_id: str,
    thread_id: str,
    action_taken: str,
    details: str | None = None,
) -> EmailProcessingLog:
    existing = db.query(EmailProcessingLog).filter(EmailProcessingLog.message_id == message_id).first()
    if existing is not None:
        existing.thread_id = thread_id
        existing.action_taken = action_taken
        if details is not None:
            existing.details = details
        db.commit()
        db.refresh(existing)
        return existing

    log_entry = EmailProcessingLog(
        message_id=message_id,
        thread_id=thread_id,
        action_taken=action_taken,
        details=details,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)
    return log_entry
