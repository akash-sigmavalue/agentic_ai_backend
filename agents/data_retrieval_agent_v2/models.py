import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


SupportedModel = Literal[
    "gpt-4o-mini",
    "gpt-5.1",
    "deepseek.v3.2",
    "moonshotai.kimi-k2.5",
]


class SemanticResolutionContext(BaseModel):
    attribute_master_tables: dict[str, Any] = Field(default_factory=dict)
    distinct_database_values: dict[str, Any] = Field(default_factory=dict)
    lookup_results: dict[str, Any] = Field(default_factory=dict)


class GenerateSqlRequest(BaseModel):
    query: str = Field(..., min_length=3, description="Natural-language real estate question.")
    model: SupportedModel | None = Field(
        None,
        description="LLM model for this pipeline run. Omit to use the configured default.",
    )
    include_intermediate_stages: bool = Field(
        True, description="Include all completed agent stage JSON outputs."
    )
    semantic_context: SemanticResolutionContext = Field(
        default_factory=SemanticResolutionContext,
        description="Approved master mappings and database evidence for Stage 2.1 resolution.",
    )

    @model_validator(mode="before")
    @classmethod
    def parse_string_encoded_json(cls, data: Any) -> Any:
        """Allow clients that send a JSON object encoded as a JSON string."""
        if not isinstance(data, str):
            return data
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be a JSON object containing a query field.") from exc


class PipelineResponse(BaseModel):
    query: str
    pipeline_status: Literal["completed", "no_data", "needs_clarification", "blocked"]
    message: str
    stopped_at_stage: str = ""
    clarification_question: str = ""
    next_action: str = ""
    sql_build_output: dict[str, Any] | None = None
    sql_review_output: dict[str, Any] | None = None
    sql_probe_output: dict[str, Any] | None = None
    sql_observe_output: dict[str, Any] | None = None
    sql_fix_output: dict[str, Any] | None = None
    sql_final_output: dict[str, Any] | None = None
    react_iterations: list[dict[str, Any]] | None = None
    token_count: dict[str, Any] | None = None
    stages: dict[str, dict[str, Any]] | None = None
