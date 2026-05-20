from __future__ import annotations

"""
Project Query Builder Models
============================
Enums and dataclasses for the Project ReAct query builder.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Enums & data classes
# ══════════════════════════════════════════════════════════════════════════════

class StepStatus(str, Enum):
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
    GOOD              = "good"
    EMPTY             = "empty"
    DB_ERROR          = "db_error"
    WRONG_COLUMN      = "wrong_column"
    WRONG_GRANULARITY = "wrong_gran"
    IRRELEVANT        = "irrelevant"


@dataclass
class Iteration:
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
    sql:         str
    rows:        list[dict]
    intent:      dict
    iterations:  int
    trace:       list[Iteration]
    success:     bool
    usage:       Any                   = None
    error:       str | None            = None


# ══════════════════════════════════════════════════════════════════════════════

