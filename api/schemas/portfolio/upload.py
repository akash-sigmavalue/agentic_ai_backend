from pydantic import BaseModel, Field


class MappingItem(BaseModel):
    uploaded_column: str
    target_field: str | None = None
    confidence: float = 0
    status: str = "custom_field"
    reason: str = ""


class MappingTableUpdate(BaseModel):
    table_index: int
    section_key: str | None = None
    mappings: list[MappingItem] = Field(default_factory=list)
    unmapped_columns: list[MappingItem] = Field(default_factory=list)


class MappingUpdateRequest(BaseModel):
    tables: list[MappingTableUpdate] = Field(default_factory=list)
