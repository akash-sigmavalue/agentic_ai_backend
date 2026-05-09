from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from database.db import Base


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"
    __table_args__ = (
        UniqueConstraint("automation_rule_id", "thread_id", name="uq_processed_emails_rule_thread"),
    )

    id = Column(Integer, primary_key=True, index=True)
    automation_rule_id = Column(Integer, ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=False, index=True)
    thread_id = Column(String, nullable=False, index=True)
    message_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
