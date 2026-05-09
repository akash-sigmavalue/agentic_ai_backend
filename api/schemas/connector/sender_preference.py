from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from database.db import Base


class SenderPreference(Base):
    __tablename__ = "sender_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "sender_email", name="uq_sender_preferences_user_sender"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_email = Column(String, nullable=False, index=True)
    tone = Column(String, nullable=True)
    auto_send_allowed = Column(Boolean, nullable=False, default=False)
    trust_level = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
