from typing import Literal

from pydantic import BaseModel, Field


MappingStatus = Literal["auto_mapped", "needs_review", "custom_field", "confirmed"]


class ColumnMappingItem(BaseModel):
    uploaded_column: str
    target_field: str | None = None
    confidence: float = 0.0
    status: MappingStatus = "custom_field"
    reason: str = ""


class MappingAgentResult(BaseModel):
    section_key: str | None = None
    section_confidence: float = 0.0
    mappings: list[ColumnMappingItem] = Field(default_factory=list)
    unmapped_columns: list[ColumnMappingItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SectionMappingResult(BaseModel):
    section_key: str
    section_confidence: float = 0.0
    mappings: list[ColumnMappingItem] = Field(default_factory=list)
    unmapped_columns: list[ColumnMappingItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MultiSectionMappingAgentResult(MappingAgentResult):
    section_mappings: list[SectionMappingResult] = Field(default_factory=list)
