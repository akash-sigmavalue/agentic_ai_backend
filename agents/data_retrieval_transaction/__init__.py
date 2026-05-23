"""
Transaction Agent Package
=========================
Transaction agent package exports for the ReAct SQL loop.
"""

from agents.data_retrieval_transaction.constants import (
    MAX_ITERATIONS,
    REVIEW_SAMPLE,
    SPACE_FILTER_FIELD_ORDER,
    SPACE_OPTION_TO_FIELD,
)
from agents.data_retrieval_transaction.helpers import (
    clean_sql,
    contains_named_entity,
    contains_space_value,
    extract_filter_columns,
    extract_space_metadata_filters,
    infer_space_filters,
    intent_has_space_context,
    merge_space_filters,
    parse_json,
    validate_select_only,
)
from agents.data_retrieval_transaction.models import (
    Iteration,
    ObserveVerdict,
    QueryResult,
    StepStatus,
)
from agents.data_retrieval_transaction.pipeline import TransactionDomainAgent
from agents.data_retrieval_transaction.prompts import (
    SQL_BUILD_PROMPT,
    SQL_FIX_PROMPT,
    SQL_OBSERVE_PROMPT,
    SQL_PROBE_PROMPT,
    SQL_REFLECT_PROMPT,
    SQL_REVIEW_PROMPT,
)
from agents.data_retrieval_transaction.query_builder import (
    TransactionQueryBuilder,
    run_query,
)

__all__ = [
    # Main agent
    "TransactionDomainAgent",
    # Constants
    "MAX_ITERATIONS",
    "REVIEW_SAMPLE",
    "SPACE_FILTER_FIELD_ORDER",
    "SPACE_OPTION_TO_FIELD",
    # Enums & Models
    "StepStatus",
    "ObserveVerdict",
    "Iteration",
    "QueryResult",
    # Classes
    "TransactionQueryBuilder",
    # Functions
    "run_query",
    # Helpers
    "clean_sql",
    "extract_filter_columns",
    "parse_json",
    "validate_select_only",
    "extract_space_metadata_filters",
    "infer_space_filters",
    "merge_space_filters",
    "contains_space_value",
    "contains_named_entity",
    "intent_has_space_context",
    # Prompts
    "SQL_BUILD_PROMPT",
    "SQL_FIX_PROMPT",
    "SQL_OBSERVE_PROMPT",
    "SQL_PROBE_PROMPT",
    "SQL_REFLECT_PROMPT",
    "SQL_REVIEW_PROMPT",
]
