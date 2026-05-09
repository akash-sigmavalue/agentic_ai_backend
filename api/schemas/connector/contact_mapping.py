from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from database.db import Base


class ContactMapping(Base):
    __tablename__ = "contact_mappings"
    __table_args__ = (
        UniqueConstraint("user_id", "display_name", "connector_type", name="uq_contact_mappings_user_name_connector"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    display_name = Column(String, nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    connector_type = Column(String, nullable=False, index=True, default="gmail")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
