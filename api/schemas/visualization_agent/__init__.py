"""
Pydantic request/response models for visualization agent Module 1 API.
"""

from typing import Any, Dict, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from agents.visualization_agent.services.constants import DEFAULT_MODEL


class Module1Request(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_query: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("user_query", "query"),
        description="The user's natural language query",
    )
    model: str = Field(default=DEFAULT_MODEL, description="OpenAI model to use")
    demo_mode: bool = Field(default=False, description="If true, return demo output without calling OpenAI")

    @field_validator("user_query")
    @classmethod
    def validate_user_query(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("user_query cannot be empty.")
        return query


class TokenLedgerRow(BaseModel):
    request_id: int = Field(..., description="Sequential request number for this backend process")
    timestamp: str = Field(..., description="Request timestamp")
    model: str = Field(..., description="OpenAI model used")
    query_preview: str = Field(..., description="Short query preview")
    input_tokens: int = Field(..., description="Input token count")
    cached_input_tokens: int = Field(..., description="Cached input token count")
    output_tokens: int = Field(..., description="Output token count")
    total_tokens: int = Field(..., description="Total token count")
    total_cost_usd: float = Field(..., description="Total estimated cost in USD")


class Module1Response(BaseModel):
    intent_output: Dict[str, Any] = Field(..., description="Full repaired Module 1 intent JSON")
    usage: Dict[str, int] = Field(..., description="Token usage breakdown")
    cost: Dict[str, float] = Field(..., description="Cost breakdown in USD")
    elapsed_seconds: float = Field(..., description="Time taken for processing")
    ledger_row: Optional[TokenLedgerRow] = Field(
        default=None,
        description="Standalone-compatible token ledger row. Demo mode calls are not counted.",
    )


class Module2InputsConsidered(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    retrieved_data: bool = Field(default=True, description="Always true – retrieved data is mandatory")
    data_mapping: bool = Field(default=True, description="Use data mapping file")
    module_1_intent: bool = Field(default=True, description="Use Module 1 intent JSON")
    retrieval_model_intent: bool = Field(default=True, description="Use retrieval model intent JSON")
    retrieval_sql_query: bool = Field(default=False, description="Use retrieval SQL query")

    @field_validator("retrieved_data")
    @classmethod
    def validate_retrieved_data_enabled(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("retrieved_data must be true because retrieved data is mandatory for Module 2.")
        return value


class Module2Request(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    inputs_considered: Module2InputsConsidered = Field(
        default_factory=Module2InputsConsidered,
        description="Which input sources to consider",
    )
    retrieved_data_path: str = Field(default="", description="Override path for retrieved data .xlsx")
    data_mapping_path: str = Field(default="", description="Override path for data mapping .py")
    module_1_intent_path: str = Field(default="", description="Override path for Module 1 intent .json")
    module_1_intent_json: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Runtime Module 1 intent JSON. When provided, this takes precedence over module_1_intent_path.",
    )
    retrieval_context_path: str = Field(default="", description="Override path for retrieval context .json")
    retrieval_sql_path: str = Field(default="", description="Override path for retrieval SQL .sql")


class Module2Response(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    module_number: int = Field(default=2)
    module_name: str = Field(default="Data Restructuring & Filtering")
    status: str = Field(..., description="success | map_not_ready | missing_required_fields | missing_metric_logic")
    next_module_ready: bool = Field(..., description="Whether downstream modules can proceed")
    source_type: Optional[str] = None
    row_limit_applied: Optional[bool] = None
    inputs_considered: Optional[Dict[str, bool]] = None
    processing_time_seconds: Optional[float] = None
    input_summary: Optional[Dict[str, Any]] = None
    mapped_fields: Optional[Dict[str, Any]] = None
    filter_validation: Optional[Dict[str, Any]] = None
    aggregation_summary: Optional[Dict[str, Any]] = None
    analysis_ready_dataset: Optional[list] = None
    visualization_ready_output: Optional[Dict[str, Any]] = None
    map_readiness: Optional[Dict[str, Any]] = None
    data_quality_summary: Optional[Dict[str, Any]] = None
    debug_metadata: Optional[Dict[str, Any]] = None
    missing_required_fields: Optional[Any] = None
    missing_metric_logic: Optional[Any] = None
    available_columns: Optional[list] = None
    available_mapping: Optional[Dict[str, Any]] = None


class Module31Request(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    module_1_intent_json: Dict[str, Any] = Field(..., description="Runtime Module 1 intent JSON")
    module_2_output_json: Dict[str, Any] = Field(..., description="Runtime Module 2 output JSON")
    model: str = Field(default=DEFAULT_MODEL, description="OpenAI model to use for all three Module 3.1 calls")


class Module31Response(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    module_number: float = Field(default=3.1)
    module_name: str = Field(default="Dynamic Map Builder")
    status: str = Field(..., description="success")
    llm_call_count: int = Field(..., description="Number of LLM calls made by Module 3.1")
    processing_time_seconds: float = Field(..., description="Total Module 3.1 processing time")
    input_summary: Dict[str, Any] = Field(..., description="Summarized Module 1 + Module 2 inputs sent to the LLM")
    planner_output: Dict[str, Any] = Field(..., description="LLM call 1 output")
    renderer_output: Dict[str, Any] = Field(..., description="LLM call 2 output")
    validator_output: Dict[str, Any] = Field(..., description="LLM call 3 output")
    final_renderer_spec: Dict[str, Any] = Field(..., description="Validated renderer spec used by approved runtime templates")
    generated_code_artifact: Dict[str, Any] = Field(..., description="Generated code artifact for inspection, not direct execution")
    usage: Dict[str, Any] = Field(..., description="Token/cost ledger across the three LLM calls")
    cache_policy: str = Field(..., description="Where the generated map should be cached")
