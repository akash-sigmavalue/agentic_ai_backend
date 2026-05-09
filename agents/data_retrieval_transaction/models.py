"""
Transaction Query Builder Models
=================================
Enums and data structures for the ReAct SQL pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    """Status enum for ReAct pipeline steps."""
    EXTRACT  = "extract"
    BUILD    = "build"
    REVIEW   = "review"
    EXECUTE  = "execute"
    OBSERVE  = "observe"
    REFLECT  = "reflect"
    REWRITE  = "rewrite"
    DONE     = "done"
    FAILED   = "failed"


class ObserveVerdict(str, Enum):
    """Verdict enum from the OBSERVE step."""
    GOOD              = "good"
    EMPTY             = "empty"
    DB_ERROR          = "db_error"
    WRONG_COLUMN      = "wrong_column"
    WRONG_GRANULARITY = "wrong_gran"
    IRRELEVANT        = "irrelevant"


@dataclass
class Iteration:
    """
    Represents a single iteration in the ReAct loop.
    
    Tracks all diagnostic and execution information for transparency and debugging.
    """
    index:          int
    sql:            str
    status:         StepStatus
    error:          str | None            = None
    rows:           list[dict]            = field(default_factory=list)
    verdict:        ObserveVerdict | None = None
    review:         dict                  = field(default_factory=dict)
    reflect:        dict                  = field(default_factory=dict)
    columns_tried:  list[str]             = field(default_factory=list)
    duration_ms:    int                   = 0
    usage:          Any                   = None


@dataclass
class QueryResult:
    """
    Final result from TransactionQueryBuilder.run().
    
    Attributes:
        sql: The final SQL query (may be empty on error)
        rows: Result rows from the query execution
        intent: The original extracted intent dict
        iterations: Number of ReAct loop iterations run
        trace: Full trace of all iterations
        success: Whether a GOOD verdict was reached
        usage: Token usage statistics (optional)
        error: Error message if success=False
    """
    sql:         str
    rows:        list[dict]
    intent:      dict
    iterations:  int
    trace:       list[Iteration]
    success:     bool
    usage:       Any                   = None
    error:       str | None            = None
