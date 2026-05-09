from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func
# from app.db.database import Base
# from app.models.automation_rule import AutomationRule  # noqa: F401
# from app.models.automation_execution_log import AutomationExecutionLog  # noqa: F401
# from app.models.contact_mapping import ContactMapping  # noqa: F401
# from app.models.email_processing_log import EmailProcessingLog  # noqa: F401
# from app.models.processed_email import ProcessedEmail  # noqa: F401
# from app.models.sender_preference import SenderPreference
from database.db import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)



class OAuthConnection(Base):
    __tablename__ = "oauth_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "system", name="uq_oauth_connections_user_provider_system"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    system = Column(String, nullable=False, index=True)
    email = Column(String, nullable=True, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    scope = Column(Text, nullable=True)
    gmail_connector_id = Column(String, nullable=True, index=True)
    gmail_history_id = Column(String, nullable=True, index=True)
    gmail_watch_expiration = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
