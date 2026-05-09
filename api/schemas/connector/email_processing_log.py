from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from database.db import Base


class EmailProcessingLog(Base):
    __tablename__ = "email_processing_log"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_email_processing_log_message_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String, nullable=False, index=True)
    thread_id = Column(String, nullable=False, index=True)
    action_taken = Column(String, nullable=False, default="processed")
    details = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
