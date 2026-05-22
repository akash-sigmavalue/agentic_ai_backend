from typing import Any

from pydantic import BaseModel, Field


class RecordUpsertRequest(BaseModel):
    record_data: dict[str, Any] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class RecordResponse(BaseModel):
    id: int
    asset_id: str | None = None
    record_data: dict[str, Any]
    custom_fields: dict[str, Any]
    derived_audit: dict[str, Any] = Field(default_factory=dict)
    validation_status: str
    validation_errors: list[Any]
