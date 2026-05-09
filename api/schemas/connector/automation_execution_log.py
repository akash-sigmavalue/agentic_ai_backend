from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from database.db import Base


class AutomationExecutionLog(Base):
    __tablename__ = "automation_execution_logs"

    id = Column(Integer, primary_key=True, index=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    connector_type = Column(String, nullable=False, index=True, default="gmail")
    trigger_event_id = Column(String, nullable=True, index=True)
    matched_message_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    action_taken = Column(String, nullable=False, index=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
