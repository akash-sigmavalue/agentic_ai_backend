from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.sql import func

from database.db import Base


class AutomationRule(Base):
    __tablename__ = "automation_rules"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    connector = Column(String, nullable=False, index=True, default="gmail")
    trigger_type = Column(String, nullable=False, index=True)
    trigger_filters = Column(JSON, nullable=False, default=dict)
    actions = Column(JSON, nullable=False, default=list)
    connector_type = Column(String, nullable=True, index=True)
    sender_name = Column(String, nullable=True, index=True)
    sender_email = Column(String, nullable=True, index=True)
    subject_filter = Column(String, nullable=True, index=True)
    keyword_filter = Column(JSON, nullable=True, default=list)
    operation = Column(String, nullable=True, index=True)
    tone = Column(String, nullable=True, index=True)
    output_requirement = Column(JSON, nullable=True, default=dict)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    last_processed_message_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
