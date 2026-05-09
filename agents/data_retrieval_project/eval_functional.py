"""
Functional Evaluation Framework for Project SQL Agent  —  v1.0
==============================================================

Evaluates THREE layers of your agent:
  Layer A  — IntentExtractor  (NL → structured intent)
  Layer B  — SQL Quality      (entity completeness, patterns, safety)
  Layer C  — End-to-end loop  (iterations, success, result relevance)

Run modes
─────────
  # Full end-to-end (requires live DB + OpenAI key)
  python eval_functional.py --mode full

  # Intent layer only (no DB needed, cheaper)
  python eval_functional.py --mode intent

  # SQL layer only (uses mock executor, no DB needed)
  python eval_functional.py --mode sql

  # With LangSmith tracing
  python eval_functional.py --mode full --langsmith

Usage
─────
  Set environment variables before running:
    OPENAI_API_KEY=sk-...
    DATABASE_URL=postgresql://user:pass@host/db        (for full mode)
    LANGSMITH_API_KEY=ls__...                          (optional)
    LANGSMITH_PROJECT=project-sql-eval                 (optional)

  Or edit CONFIG at the bottom of this file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── Try importing optional deps gracefully ────────────────────────────────────
try:
    from openai import OpenAI
except ImportError:
    print("[ERROR] openai not installed. Run: pip install openai")
    sys.exit(1)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    print("[WARN] numpy not installed — cosine similarity scoring disabled.")

try:
    from langsmith import Client as LangSmithClient
    from langsmith import traceable
    HAS_LANGSMITH = True
except ImportError:
    HAS_LANGSMITH = False
    print("[WARN] langsmith not installed — LangSmith mode disabled.")

# ── Your agent imports ────────────────────────────────────────────────────────
try:
    from agents.data_retrieval_project.query_builder import (
        IntentExtractor,
        ProjectQueryBuilder,
        run_query,
        QueryResult,
    )
    from agents.data_retrieval_project.schema import PROJECT_QUERY_SCHEMA
    AGENT_IMPORTED = True
except ImportError as e:
    print(f"[WARN] Could not import agent: {e}")
    print("       Running in MOCK mode — SQL/end-to-end tests will use stubs.")
    AGENT_IMPORTED = False
    PROJECT_QUERY_SCHEMA = "mock_schema"

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — TEST CASES
# ══════════════════════════════════════════════════════════════════════════════
# Each test case is a dict. Fields explained inline below.
# Add/remove cases freely — the runner picks them all up automatically.

TEST_CASES: list[dict] = [
    {
        "id": "PC_001",
        "category": "basic",
        "query": "total units in Baner projects",
        "expected_intent": {
            "analysis_type": "summary",
            "entities": {
                "locations": [{"value": "Baner", "semantic_level": "locality"}]
            },
            "metrics": [{"alias": "total_units"}],
        },
        "expected_sql_patterns": [
            r"FROM\s+projects",
            r"SUM\s*\(\s*total_units\s*\)",
            r"ILIKE\s+'%Baner%'",
        ],
        "forbidden_sql_patterns": [
            r"=\s*'Baner'",
            r"\bDROP\b",
            r"\bDELETE\b",
        ],
        "min_row_count": 1,
        "max_iterations_expected": 3,
    },
    {
        "id": "PC_002",
        "category": "entity_completeness",
        "query": "compare Baner and Hinjewadi by total units available units and booking rate",
        "expected_intent": {
            "analysis_type": "comparison",
            "entities": {
                "locations": [
                    {"value": "Baner", "semantic_level": "locality"},
                    {"value": "Hinjewadi", "semantic_level": "locality"},
                ]
            },
            "metrics": [
                {"alias": "total_units"},
                {"alias": "available_units"},
                {"alias": "booking_rate"},
            ],
        },
        "expected_sql_patterns": [
            r"ILIKE\s+'%Baner%'",
            r"ILIKE\s+'%Hinjewadi%'",
            r"SUM\s*\(\s*total_units\s*\)",
            r"booked_units",
            r"NULLIF",
            r"GROUP\s+BY",
        ],
        "forbidden_sql_patterns": [
            r"=\s*'Baner'",
            r"=\s*'Hinjewadi'",
        ],
        "min_row_count": 1,
        "max_iterations_expected": 4,
    },
    {
        "id": "PC_003",
        "category": "entity_completeness",
        "query": "compare Baner, Wakad, and Kothrud by project count",
        "expected_intent": {
            "analysis_type": "comparison",
            "entities": {
                "locations": [
                    {"value": "Baner", "semantic_level": "locality"},
                    {"value": "Wakad", "semantic_level": "locality"},
                    {"value": "Kothrud", "semantic_level": "locality"},
                ]
            },
            "metrics": [{"alias": "project_count"}],
        },
        "expected_sql_patterns": [
            r"ILIKE\s+'%Baner%'",
            r"ILIKE\s+'%Wakad%'",
            r"ILIKE\s+'%Kothrud%'",
            r"COUNT\s*\(\s*\*\s*\)",
            r"GROUP\s+BY",
        ],
        "forbidden_sql_patterns": [],
        "min_row_count": 1,
        "max_iterations_expected": 4,
    },
    {
        "id": "PC_004",
        "category": "trend",
        "query": "show quarterly project commencement trend in Wakad",
        "expected_intent": {
            "analysis_type": "trend",
            "time_series": True,
            "entities": {
                "locations": [{"value": "Wakad", "semantic_level": "locality"}]
            },
        },
        "expected_sql_patterns": [
            r"GROUP\s+BY",
            r"\b(year|quarter|EXTRACT|DATE_TRUNC|commencement_date)\b",
            r"ORDER\s+BY",
            r"ILIKE\s+'%Wakad%'",
        ],
        "forbidden_sql_patterns": [],
        "min_row_count": 2,
        "max_iterations_expected": 4,
    },
    {
        "id": "PC_005",
        "category": "ranking",
        "query": "top 5 localities by project count in Pune",
        "expected_intent": {
            "analysis_type": "ranking",
            "entities": {
                "locations": [{"value": "Pune", "semantic_level": "city"}],
                "limit": 5,
            },
            "metrics": [{"alias": "project_count"}],
        },
        "expected_sql_patterns": [
            r"LIMIT\s+5",
            r"ORDER\s+BY",
            r"DESC",
            r"COUNT\s*\(\s*\*\s*\)",
            r"GROUP\s+BY",
            r"ILIKE\s+'%Pune%'",
        ],
        "forbidden_sql_patterns": [],
        "min_row_count": 1,
        "max_iterations_expected": 3,
    },
    {
        "id": "PC_006",
        "category": "metric_formula",
        "query": "what is the booking rate for Hinjewadi projects",
        "expected_intent": {
            "analysis_type": "summary",
            "entities": {
                "locations": [{"value": "Hinjewadi", "semantic_level": "locality"}]
            },
            "metrics": [{"alias": "booking_rate"}],
        },
        "expected_sql_patterns": [
            r"booked_units",
            r"total_units",
            r"NULLIF",
            r"ROUND",
        ],
        "forbidden_sql_patterns": [],
        "min_row_count": 1,
        "max_iterations_expected": 3,
    },
    {
        "id": "PC_007",
        "category": "status_filter",
        "query": "under construction projects in Baner by total units",
        "expected_intent": {
            "analysis_type": "summary",
            "entities": {
                "locations": [{"value": "Baner", "semantic_level": "locality"}]
            },
            "filters": {"construction_status": "under construction"},
            "metrics": [{"alias": "total_units"}],
        },
        "expected_sql_patterns": [
            r"construction_status\s+ILIKE\s+'%under construction%'",
            r"SUM\s*\(\s*total_units\s*\)",
            r"ILIKE\s+'%Baner%'",
        ],
        "forbidden_sql_patterns": [
            r"construction_status\s*=\s*'under construction'",
        ],
        "min_row_count": 0,
        "max_iterations_expected": 3,
    },
    {
        "id": "PC_008",
        "category": "ilike_enforcement",
        "query": "projects in Kharadi",
        "expected_intent": {
            "analysis_type": "lookup",
            "entities": {
                "locations": [{"value": "Kharadi", "semantic_level": "locality"}]
            },
        },
        "expected_sql_patterns": [
            r"ILIKE",
            r"project_name|registered_project_name",
        ],
        "forbidden_sql_patterns": [
            r"=\s*'[Kk]haradi'",
        ],
        "min_row_count": 0,
        "max_iterations_expected": 3,
    },
    {
        "id": "PC_009",
        "category": "multi_metric",
        "query": "give me total units available units booking rate and plot area sqft for Aundh",
        "expected_intent": {
            "analysis_type": "summary",
            "entities": {
                "locations": [{"value": "Aundh", "semantic_level": "locality"}]
            },
            "metrics": [
                {"alias": "total_units"},
                {"alias": "available_units"},
                {"alias": "booking_rate"},
                {"alias": "plot_area_sqft"},
            ],
        },
        "expected_sql_patterns": [
            r"SUM\s*\(\s*total_units\s*\)",
            r"booked_units",
            r"total_plot_area_sq_m",
            r"10\.764",
            r"ILIKE\s+'%Aundh%'",
        ],
        "forbidden_sql_patterns": [],
        "min_row_count": 0,
        "max_iterations_expected": 3,
    },
    {
        "id": "PC_010",
        "category": "distribution",
        "query": "breakdown of projects by construction status in Pune",
        "expected_intent": {
            "analysis_type": "distribution",
            "entities": {
                "locations": [{"value": "Pune", "semantic_level": "city"}]
            },
        },
        "expected_sql_patterns": [
            r"construction_status",
            r"GROUP\s+BY",
            r"COUNT\s*\(\s*\*\s*\)",
            r"ILIKE\s+'%Pune%'",
        ],
        "forbidden_sql_patterns": [],
        "min_row_count": 1,
        "max_iterations_expected": 3,
    },
    {
        "id": "PC_011",
        "category": "developer",
        "query": "list Kolte Patil projects in Pune",
        "expected_intent": {
            "analysis_type": "lookup",
            "entities": {
                "locations": [{"value": "Pune", "semantic_level": "city"}],
                "developers": [{"value": "Kolte Patil", "semantic_level": "developer"}],
            },
        },
        "expected_sql_patterns": [
            r"organization_individual_name\s+ILIKE\s+'%Kolte Patil%'",
            r"ILIKE\s+'%Pune%'",
            r"LIMIT",
        ],
        "forbidden_sql_patterns": [
            r"organization_individual_name\s*=\s*'Kolte Patil'",
        ],
        "min_row_count": 0,
        "max_iterations_expected": 3,
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — RESULT DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class IntentEvalResult:
    """Score for Layer A: IntentExtractor."""
    test_id:               str
    query:                 str
    passed:                bool
    analysis_type_correct: bool
    entity_recall:         float    # fraction of expected entities found
    entity_precision:      float    # fraction of predicted entities that were expected
    metric_recall:         float    # fraction of expected metrics found
    f1_entity:             float    # harmonic mean of recall + precision
    issues:                list[str] = field(default_factory=list)
    predicted_intent:      dict      = field(default_factory=dict)
    duration_ms:           int       = 0


@dataclass
class SQLEvalResult:
    """Score for Layer B: SQL Quality."""
    test_id:                  str
    query:                    str
    passed:                   bool
    patterns_matched:         dict[str, bool]   # pattern → found?
    forbidden_matched:        dict[str, bool]   # pattern → found? (should be False)
    entity_completeness_score: float             # 0–1
    is_select_only:           bool
    has_group_by_when_needed: bool
    issues:                   list[str] = field(default_factory=list)
    sql:                      str       = ""
    duration_ms:              int       = 0


@dataclass
class EndToEndEvalResult:
    """Score for Layer C: Full ReAct loop."""
    test_id:            str
    query:              str
    passed:             bool
    agent_success:      bool      # result.success from QueryResult
    iterations_used:    int
    iterations_ok:      bool      # within max_iterations_expected?
    row_count:          int
    row_count_ok:       bool      # >= min_row_count?
    relevance_score:    float     # cosine similarity (0–1), -1 if unavailable
    intent_score:       float     # from IntentEvalResult.f1_entity
    sql_score:          float     # fraction of expected patterns matched
    overall_score:      float     # weighted composite
    issues:             list[str] = field(default_factory=list)
    sql:                str       = ""
    rows:               list[dict] = field(default_factory=list)
    duration_ms:        int       = 0


@dataclass
class EvalReport:
    """Aggregated report across all test cases."""
    timestamp:          str
    mode:               str
    total_cases:        int
    # Layer A
    intent_pass_rate:   float
    avg_entity_recall:  float
    avg_metric_recall:  float
    avg_f1_entity:      float
    # Layer B
    sql_pass_rate:      float
    avg_pattern_score:  float
    ilike_compliance:   float     # % of cases using ILIKE correctly
    # Layer C
    e2e_pass_rate:      float
    avg_iterations:     float
    avg_relevance:      float
    avg_overall_score:  float
    # Raw
    intent_results:     list[IntentEvalResult]  = field(default_factory=list)
    sql_results:        list[SQLEvalResult]     = field(default_factory=list)
    e2e_results:        list[EndToEndEvalResult] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — LAYER A: INTENT EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════

class IntentEvaluator:
    """
    Evaluates IntentExtractor in isolation.

    Calls extractor.extract(query) and compares against expected_intent.
    Uses field-level scoring — NOT cosine similarity on the whole dict.
    Cosine sim is unreliable for structured JSON (a missing field scores
    high because surrounding text matches).
    """

    def __init__(self, extractor: Any) -> None:
        self.extractor = extractor

    @staticmethod
    def _expected_entity_values(intent: dict) -> set[str]:
        entities = intent.get("entities") or {}
        values: set[str] = set()
        for key in ("locations", "projects", "developers", "property_types"):
            for entity in entities.get(key) or []:
                value = entity.get("value") if isinstance(entity, dict) else entity
                if value:
                    values.add(str(value).lower())
        return values

    def evaluate(self, test_case: dict) -> IntentEvalResult:
        t0    = time.monotonic()
        query = test_case["query"]
        exp   = test_case.get("expected_intent", {})
        issues: list[str] = []

        # ── Call the extractor ─────────────────────────────────────────────
        try:
            predicted = self.extractor.extract(query)
        except Exception as e:
            return IntentEvalResult(
                test_id=test_case["id"],
                query=query,
                passed=False,
                analysis_type_correct=False,
                entity_recall=0.0,
                entity_precision=0.0,
                metric_recall=0.0,
                f1_entity=0.0,
                issues=[f"Extractor raised exception: {e}"],
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── analysis_type ──────────────────────────────────────────────────
        exp_type  = exp.get("analysis_type")
        pred_type = predicted.get("analysis_type")
        type_ok   = (exp_type is None) or (exp_type == pred_type)
        if not type_ok:
            issues.append(
                f"analysis_type: expected '{exp_type}', got '{pred_type}'"
            )

        # ── Entity recall & precision ──────────────────────────────────────
        # Compare all project-domain entity values case-insensitively.
        exp_entities = self._expected_entity_values(exp)
        pred_entities = self._expected_entity_values(predicted)

        if exp_entities:
            tp = len(exp_entities & pred_entities)
            entity_recall = tp / len(exp_entities)
            entity_precision = tp / max(len(pred_entities), 1)
            f1 = (
                2 * entity_recall * entity_precision
                / max(entity_recall + entity_precision, 1e-9)
            )
            missing = exp_entities - pred_entities
            extra = pred_entities - exp_entities
            if missing:
                issues.append(f"Entities missing from intent: {missing}")
            if extra:
                issues.append(f"Extra entities in intent (not expected): {extra}")
        else:
            entity_recall    = 1.0
            entity_precision = 1.0
            f1               = 1.0

        # ── Metric recall ──────────────────────────────────────────────────
        exp_metrics  = {m["alias"] for m in (exp.get("metrics") or [])}
        pred_metrics = {m["alias"] for m in (predicted.get("metrics") or [])}

        if exp_metrics:
            metric_recall = len(exp_metrics & pred_metrics) / len(exp_metrics)
            missing_m     = exp_metrics - pred_metrics
            if missing_m:
                issues.append(f"Metrics missing from intent: {missing_m}")
        else:
            metric_recall = 1.0

        # ── time_series check ──────────────────────────────────────────────
        if exp.get("time_series") is True and not predicted.get("time_series"):
            issues.append("time_series should be True but is False/missing")

        # ── limit check ───────────────────────────────────────────────────
        exp_limit = (exp.get("entities") or {}).get("limit")
        if exp_limit is not None:
            pred_limit = (predicted.get("entities") or {}).get("limit")
            if pred_limit != exp_limit:
                issues.append(
                    f"entities.limit: expected {exp_limit}, got {pred_limit}"
                )

        # ── Overall pass/fail ──────────────────────────────────────────────
        # Pass = correct type AND all expected entities captured AND
        #        metric recall >= 0.8 (allow minor metric miss)
        passed = type_ok and entity_recall == 1.0 and metric_recall >= 0.8

        return IntentEvalResult(
            test_id=test_case["id"],
            query=query,
            passed=passed,
            analysis_type_correct=type_ok,
            entity_recall=entity_recall,
            entity_precision=entity_precision,
            metric_recall=metric_recall,
            f1_entity=f1,
            issues=issues,
            predicted_intent=predicted,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — LAYER B: SQL EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════

class SQLEvaluator:
    """
    Evaluates SQL quality WITHOUT executing it against a real database.

    Checks:
      1. All expected regex patterns are present in SQL
      2. No forbidden patterns appear
      3. All expected entities from intent are present in WHERE clause
      4. SQL is SELECT-only (no DML)
      5. GROUP BY is present when analysis_type requires it
    """

    # Analysis types that must have GROUP BY
    GROUPBY_REQUIRED: set[str] = {
        "comparison", "trend", "ranking", "distribution"
    }

    @staticmethod
    def _entity_values(intent: dict) -> list[str]:
        entities = intent.get("entities") or {}
        values: list[str] = []
        for key in ("locations", "projects", "developers", "property_types"):
            for entity in entities.get(key) or []:
                value = entity.get("value") if isinstance(entity, dict) else entity
                if value:
                    values.append(str(value))
        return values

    def evaluate(
        self,
        test_case:       dict,
        sql:             str,
        predicted_intent: dict | None = None,
    ) -> SQLEvalResult:
        t0     = time.monotonic()
        issues: list[str] = []

        # Use predicted intent if available, else build from test case
        intent = predicted_intent or test_case.get("expected_intent", {})
        analysis_type = intent.get("analysis_type", "summary")

        # ── 1. Expected pattern matching ───────────────────────────────────
        patterns_matched: dict[str, bool] = {}
        for pattern in test_case.get("expected_sql_patterns", []):
            found = bool(re.search(pattern, sql, re.IGNORECASE))
            patterns_matched[pattern] = found
            if not found:
                issues.append(f"Expected pattern NOT found in SQL: `{pattern}`")

        # ── 2. Forbidden pattern check ─────────────────────────────────────
        forbidden_matched: dict[str, bool] = {}
        for pattern in test_case.get("forbidden_sql_patterns", []):
            found = bool(re.search(pattern, sql, re.IGNORECASE))
            forbidden_matched[pattern] = found
            if found:
                issues.append(f"Forbidden pattern FOUND in SQL: `{pattern}`")

        # ── 3. Entity completeness ─────────────────────────────────────────
        exp_locs = self._entity_values(intent)
        entity_hits: list[bool] = []
        for loc in exp_locs:
            # Entity can appear in ILIKE '%Baner%' or similar.
            found = bool(
                re.search(rf"ILIKE\s+'%{re.escape(loc)}%'", sql, re.IGNORECASE)
                or re.search(rf"'%{re.escape(loc.lower())}%'", sql, re.IGNORECASE)
            )
            entity_hits.append(found)
            if not found:
                issues.append(
                    f"Entity '{loc}' missing from SQL WHERE clause "
                    f"(not found as ILIKE pattern)"
                )

        entity_completeness = (
            sum(entity_hits) / len(entity_hits) if entity_hits else 1.0
        )

        # ── 4. SELECT-only validation ──────────────────────────────────────
        is_select = sql.strip().upper().startswith(("SELECT", "WITH"))
        blocked   = re.compile(
            r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b',
            re.IGNORECASE,
        )
        if not is_select:
            issues.append("SQL does not start with SELECT or WITH")
        if blocked.search(sql):
            issues.append("SQL contains a blocked DML/DDL keyword")
        is_select_only = is_select and not bool(blocked.search(sql))

        # ── 5. GROUP BY when required ──────────────────────────────────────
        needs_group_by   = analysis_type in self.GROUPBY_REQUIRED
        has_group_by     = bool(re.search(r'\bGROUP\s+BY\b', sql, re.IGNORECASE))
        group_by_ok      = (not needs_group_by) or has_group_by
        if needs_group_by and not has_group_by:
            issues.append(
                f"analysis_type='{analysis_type}' requires GROUP BY but it's missing"
            )

        # ── Overall pass/fail ──────────────────────────────────────────────
        all_patterns_ok = all(patterns_matched.values()) if patterns_matched else True
        no_forbidden    = not any(forbidden_matched.values())

        passed = (
            all_patterns_ok
            and no_forbidden
            and entity_completeness == 1.0
            and is_select_only
            and group_by_ok
        )

        return SQLEvalResult(
            test_id=test_case["id"],
            query=test_case["query"],
            passed=passed,
            patterns_matched=patterns_matched,
            forbidden_matched=forbidden_matched,
            entity_completeness_score=entity_completeness,
            is_select_only=is_select_only,
            has_group_by_when_needed=group_by_ok,
            issues=issues,
            sql=sql,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — LAYER C: END-TO-END EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════

class RelevanceScorer:
    """
    Scores semantic relevance of result rows against the user's query
    using OpenAI embeddings + cosine similarity.

    This is the ONLY place where cosine similarity is used.
    It answers: "Do the returned rows actually match what was asked?"
    Using it on JSON intent would be misleading — use field-level scoring there.
    """

    def __init__(self, client: OpenAI, model: str = "text-embedding-3-small") -> None:
        self.client = client
        self.model  = model

    def score(self, user_query: str, rows: list[dict]) -> float:
        """
        Returns 0.0–1.0.
        Returns -1.0 if scoring is unavailable (numpy missing or rows empty).
        """
        if not HAS_NUMPY or not rows:
            return -1.0

        # Serialize top-5 rows as readable text
        rows_text = " | ".join(
            ", ".join(f"{k}={v}" for k, v in row.items())
            for row in rows[:5]
        )

        try:
            resp = self.client.embeddings.create(
                model=self.model,
                input=[user_query, rows_text],
            )
            q_vec = np.array(resp.data[0].embedding)
            r_vec = np.array(resp.data[1].embedding)
            sim   = float(
                np.dot(q_vec, r_vec)
                / (np.linalg.norm(q_vec) * np.linalg.norm(r_vec))
            )
            # Cosine sim ranges -1..1; clamp to 0..1 for readability
            return max(0.0, sim)
        except Exception as e:
            logger.warning("RelevanceScorer failed: %s", e)
            return -1.0


class EndToEndEvaluator:
    """
    Runs the full agent (IntentExtractor + ProjectQueryBuilder ReAct loop)
    against a live DB and scores the result.
    """

    def __init__(
        self,
        client:           OpenAI,
        db_executor:      Callable[[str], list[dict]],
        relevance_scorer: RelevanceScorer,
        model:            str = "gpt-4.1",
    ) -> None:
        self.client           = client
        self.db_executor      = db_executor
        self.relevance_scorer = relevance_scorer
        self.model            = model
        self._intent_eval     = IntentEvaluator(
            IntentExtractor(client=client, model=model)
        )
        self._sql_eval        = SQLEvaluator()

    def evaluate(self, test_case: dict) -> EndToEndEvalResult:
        t0     = time.monotonic()
        issues: list[str] = []
        query  = test_case["query"]

        # ── Run full agent ────────────────────────────────────────────────
        try:
            result: QueryResult = run_query(
                user_query=query,
                client=self.client,
                db_executor=self.db_executor,
                model=self.model,
            )
        except Exception as e:
            return EndToEndEvalResult(
                test_id=test_case["id"],
                query=query,
                passed=False,
                agent_success=False,
                iterations_used=0,
                iterations_ok=False,
                row_count=0,
                row_count_ok=False,
                relevance_score=-1.0,
                intent_score=0.0,
                sql_score=0.0,
                overall_score=0.0,
                issues=[f"Agent raised exception: {e}"],
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── Sub-scores ────────────────────────────────────────────────────

        # Intent sub-score (re-evaluate the intent the agent produced)
        intent_eval   = self._intent_eval.evaluate(test_case)
        intent_score  = intent_eval.f1_entity
        if intent_eval.issues:
            issues.extend([f"[Intent] {i}" for i in intent_eval.issues])

        # SQL sub-score
        sql_eval      = self._sql_eval.evaluate(
            test_case,
            result.sql,
            predicted_intent=result.intent,
        )
        # Fraction of expected patterns matched
        matched       = sum(sql_eval.patterns_matched.values())
        total_pat     = max(len(sql_eval.patterns_matched), 1)
        sql_score     = matched / total_pat
        if sql_eval.issues:
            issues.extend([f"[SQL] {i}" for i in sql_eval.issues])

        # Iteration efficiency
        max_iters_exp = test_case.get("max_iterations_expected", 3)
        iters_ok      = result.iterations <= max_iters_exp
        if not iters_ok:
            issues.append(
                f"Used {result.iterations} iterations "
                f"(expected ≤ {max_iters_exp})"
            )

        # Row count
        min_rows      = test_case.get("min_row_count", 0)
        row_count_ok  = len(result.rows) >= min_rows
        if not row_count_ok:
            issues.append(
                f"Row count {len(result.rows)} < expected minimum {min_rows}"
            )

        # Relevance (semantic)
        relevance = self.relevance_scorer.score(query, result.rows)

        # ── Composite score ───────────────────────────────────────────────
        # Weights: intent 30%, SQL 30%, rows 20%, iterations 20%
        iter_score    = 1.0 if iters_ok else max(0.0, 1 - (result.iterations - max_iters_exp) * 0.2)
        row_score     = 1.0 if row_count_ok else 0.0
        overall       = (
            0.30 * intent_score
            + 0.30 * sql_score
            + 0.20 * row_score
            + 0.20 * iter_score
        )

        # ── Pass/fail ─────────────────────────────────────────────────────
        passed = (
            result.success
            and intent_score >= 0.8
            and sql_score    >= 0.8
            and row_count_ok
        )

        return EndToEndEvalResult(
            test_id=test_case["id"],
            query=query,
            passed=passed,
            agent_success=result.success,
            iterations_used=result.iterations,
            iterations_ok=iters_ok,
            row_count=len(result.rows),
            row_count_ok=row_count_ok,
            relevance_score=relevance,
            intent_score=intent_score,
            sql_score=sql_score,
            overall_score=overall,
            issues=issues,
            sql=result.sql,
            rows=result.rows[:3],   # save first 3 rows for report
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — LANGSMITH INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

class LangSmithEvaluator:
    """
    Uploads test cases as a LangSmith dataset, runs the agent as a target
    function, and registers custom evaluators for:
      - entity_completeness
      - iteration_efficiency
      - sql_pattern_match
      -     

    Requires: pip install langsmith
    """

    def __init__(
        self,
        openai_client:  OpenAI,
        db_executor:    Callable[[str], list[dict]],
        model:          str   = "gpt-4.1",
        dataset_name:   str   = "project-sql-agent-eval",
        project_prefix: str   = "project-react-v3",
    ) -> None:
        if not HAS_LANGSMITH:
            raise RuntimeError("langsmith package not installed.")
        self.openai_client  = openai_client
        self.db_executor    = db_executor
        self.model          = model
        self.dataset_name   = dataset_name
        self.project_prefix = project_prefix
        self.ls_client      = LangSmithClient()
        self._sql_eval      = SQLEvaluator()

    def _get_or_create_dataset(self) -> Any:
        """Create dataset if it doesn't exist, else return existing."""
        try:
            existing = self.ls_client.read_dataset(dataset_name=self.dataset_name)
            print(f"[LangSmith] Using existing dataset: {self.dataset_name}")
            return existing
        except Exception:
            pass

        dataset = self.ls_client.create_dataset(
            dataset_name=self.dataset_name,
            description="Project SQL agent functional evaluation dataset",
        )
        for tc in TEST_CASES:
            self.ls_client.create_example(
                inputs={"query": tc["query"], "test_id": tc["id"]},
                outputs={
                    "expected_intent":           tc.get("expected_intent", {}),
                    "expected_sql_patterns":     tc.get("expected_sql_patterns", []),
                    "forbidden_sql_patterns":    tc.get("forbidden_sql_patterns", []),
                    "min_row_count":             tc.get("min_row_count", 0),
                    "max_iterations_expected":   tc.get("max_iterations_expected", 3),
                },
                dataset_id=dataset.id,
            )
        print(
            f"[LangSmith] Created dataset '{self.dataset_name}' "
            f"with {len(TEST_CASES)} examples."
        )
        return dataset

    def _make_target(self):
        """
        Returns a function that LangSmith's evaluate() will call per example.
        Must accept (inputs) and return a dict.
        """
        client      = self.openai_client
        db_executor = self.db_executor
        model       = self.model

        if HAS_LANGSMITH:
            @traceable
            def agent_target(inputs: dict) -> dict:
                result = run_query(
                    user_query  = inputs["query"],
                    client      = client,
                    db_executor = db_executor,
                    model       = model,
                )
                return {
                    "sql":        result.sql,
                    "rows":       result.rows,
                    "intent":     result.intent,
                    "iterations": result.iterations,
                    "success":    result.success,
                }
        else:
            def agent_target(inputs: dict) -> dict:
                return {}

        return agent_target

    def _make_evaluators(self) -> list[Callable]:
        """Build LangSmith-compatible evaluator functions."""
        sql_eval = self._sql_eval

        def eval_entity_completeness(run, example):
            sql            = run.outputs.get("sql", "")
            expected_locs = SQLEvaluator._entity_values(
                example.outputs.get("expected_intent") or {}
            )
            missing = [
                loc for loc in expected_locs
                if not re.search(
                    rf"ILIKE\s+'%{re.escape(loc)}%'", sql, re.IGNORECASE
                )
            ]
            score = (
                1.0
                if not missing
                else (len(expected_locs) - len(missing)) / max(len(expected_locs), 1)
            )
            return {
                "key":     "entity_completeness",
                "score":   score,
                "comment": f"Missing in SQL: {missing}" if missing else "All entities found",
            }

        def eval_iteration_efficiency(run, example):
            iters     = run.outputs.get("iterations", 99)
            max_exp   = example.outputs.get("max_iterations_expected", 3)
            score     = 1.0 if iters <= max_exp else max(0.0, 1 - (iters - max_exp) * 0.25)
            return {
                "key":     "iteration_efficiency",
                "score":   score,
                "comment": f"Used {iters} iterations (expected ≤ {max_exp})",
            }

        def eval_sql_patterns(run, example):
            sql      = run.outputs.get("sql", "")
            patterns = example.outputs.get("expected_sql_patterns", [])
            if not patterns:
                return {"key": "sql_patterns", "score": 1.0, "comment": "No patterns to check"}
            hits  = [bool(re.search(p, sql, re.IGNORECASE)) for p in patterns]
            score = sum(hits) / len(hits)
            missing = [p for p, h in zip(patterns, hits) if not h]
            return {
                "key":     "sql_patterns",
                "score":   score,
                "comment": f"Missing: {missing}" if missing else "All patterns matched",
            }

        def eval_row_count(run, example):
            rows      = run.outputs.get("rows", [])
            min_rows  = example.outputs.get("min_row_count", 0)
            score     = 1.0 if len(rows) >= min_rows else 0.0
            return {
                "key":     "row_count",
                "score":   score,
                "comment": f"Got {len(rows)} rows (min expected {min_rows})",
            }

        def eval_ilike_compliance(run, example):
            sql       = run.outputs.get("sql", "")
            forbidden = example.outputs.get("forbidden_sql_patterns", [])
            violations = [p for p in forbidden if re.search(p, sql, re.IGNORECASE)]
            score      = 0.0 if violations else 1.0
            return {
                "key":     "ilike_compliance",
                "score":   score,
                "comment": f"Violations: {violations}" if violations else "No forbidden patterns",
            }

        return [
            eval_entity_completeness,
            eval_iteration_efficiency,
            eval_sql_patterns,
            eval_row_count,
            eval_ilike_compliance,
        ]

    def run(self):
        """Upload dataset + run evaluation via LangSmith."""
        from langsmith.evaluation import evaluate as ls_evaluate

        dataset    = self._get_or_create_dataset()
        target     = self._make_target()
        evaluators = self._make_evaluators()

        print(f"[LangSmith] Running evaluation on dataset '{self.dataset_name}'...")
        results = ls_evaluate(
            target,
            data=self.dataset_name,
            evaluators=evaluators,
            experiment_prefix=self.project_prefix,
        )
        print("[LangSmith] Evaluation complete. Check your LangSmith dashboard.")
        return results


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — RUNNER & REPORTER
# ══════════════════════════════════════════════════════════════════════════════

class EvalRunner:
    """
    Orchestrates evaluation across all three layers and produces a report.

    mode="intent"  → Layer A only (no DB, no SQL execution)
    mode="sql"     → Layer A + B (uses mock executor)
    mode="full"    → All three layers (requires live DB)
    """

    def __init__(
        self,
        openai_client: OpenAI,
        db_executor:   Callable[[str], list[dict]] | None = None,
        model:         str = "gpt-4.1",
        mode:          str = "full",
    ) -> None:
        self.client      = openai_client
        self.db_executor = db_executor or self._mock_executor
        self.model       = model
        self.mode        = mode

        if AGENT_IMPORTED:
            self._extractor  = IntentExtractor(client=openai_client, model=model)
            self._intent_eval = IntentEvaluator(self._extractor)
        else:
            self._extractor   = None
            self._intent_eval = None

        self._sql_eval   = SQLEvaluator()
        self._relevance  = RelevanceScorer(openai_client)

    @staticmethod
    def _mock_executor(sql: str) -> list[dict]:
        """
        Mock DB executor for sql-only mode.
        Returns plausible fake rows so the agent loop can proceed.
        """
        return [
            {
                "location_name": "Baner",
                "project_count": 24,
                "total_units": 1800,
                "available_units": 420,
                "booking_rate": 76.67,
            },
            {
                "location_name": "Hinjewadi",
                "project_count": 18,
                "total_units": 1320,
                "available_units": 310,
                "booking_rate": 76.52,
            },
        ]

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(self, test_cases: list[dict] | None = None) -> EvalReport:
        cases = test_cases or TEST_CASES
        print(f"\n{'='*60}")
        print(f" Project SQL Agent — Functional Evaluation  ({self.mode.upper()} mode)")
        print(f" Cases: {len(cases)}   Model: {self.model}")
        print(f"{'='*60}\n")

        intent_results: list[IntentEvalResult]   = []
        sql_results:    list[SQLEvalResult]       = []
        e2e_results:    list[EndToEndEvalResult]  = []

        for i, tc in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {tc['id']} — {tc['query'][:65]}")

            # ── Layer A: Intent ────────────────────────────────────────────
            if self._intent_eval and AGENT_IMPORTED:
                ir = self._intent_eval.evaluate(tc)
                intent_results.append(ir)
                _print_layer_result("Intent", ir.passed, ir.issues)
            else:
                print("  [Intent] SKIPPED — agent not imported")

            # ── Layer B: SQL ───────────────────────────────────────────────
            if self.mode in ("sql", "full") and AGENT_IMPORTED:
                try:
                    builder = ProjectQueryBuilder(
                        client=self.client,
                        db_executor=self.db_executor,
                        model=self.model,
                    )
                    intent = (
                        intent_results[-1].predicted_intent
                        if intent_results
                        else tc.get("expected_intent", {})
                    )
                    sql_str = builder.build(intent)
                    sr      = self._sql_eval.evaluate(tc, sql_str, predicted_intent=intent)
                    sql_results.append(sr)
                    _print_layer_result("SQL", sr.passed, sr.issues)
                except Exception as e:
                    print(f"  [SQL] ERROR — {e}")
            else:
                print(f"  [SQL] SKIPPED (mode={self.mode})")

            # ── Layer C: End-to-end ────────────────────────────────────────
            if self.mode == "full" and AGENT_IMPORTED:
                e2e_eval = EndToEndEvaluator(
                    client=self.client,
                    db_executor=self.db_executor,
                    relevance_scorer=self._relevance,
                    model=self.model,
                )
                er = e2e_eval.evaluate(tc)
                e2e_results.append(er)
                _print_layer_result(
                    "E2E",
                    er.passed,
                    er.issues,
                    extra=f"iters={er.iterations_used} rows={er.row_count} score={er.overall_score:.2f}",
                )
            else:
                print(f"  [E2E] SKIPPED (mode={self.mode})")

            print()

        return self._build_report(intent_results, sql_results, e2e_results)

    # ── Report builder ─────────────────────────────────────────────────────────

    def _build_report(
        self,
        ir: list[IntentEvalResult],
        sr: list[SQLEvalResult],
        er: list[EndToEndEvalResult],
    ) -> EvalReport:

        def _avg(lst, key):
            vals = [getattr(x, key) for x in lst]
            return sum(vals) / len(vals) if vals else 0.0

        def _pass_rate(lst):
            return sum(x.passed for x in lst) / len(lst) if lst else 0.0

        # SQL pattern score across all cases
        sql_pattern_scores = []
        for r in sr:
            total = len(r.patterns_matched)
            if total:
                sql_pattern_scores.append(sum(r.patterns_matched.values()) / total)

        # ILIKE compliance = fraction of cases with no forbidden pattern violations
        ilike_compliance = (
            sum(1 for r in sr if not any(r.forbidden_matched.values()))
            / max(len(sr), 1)
        )

        return EvalReport(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            mode=self.mode,
            total_cases=max(len(ir), len(sr), len(er)),
            # Layer A
            intent_pass_rate=_pass_rate(ir),
            avg_entity_recall=_avg(ir, "entity_recall"),
            avg_metric_recall=_avg(ir, "metric_recall"),
            avg_f1_entity=_avg(ir, "f1_entity"),
            # Layer B
            sql_pass_rate=_pass_rate(sr),
            avg_pattern_score=sum(sql_pattern_scores) / max(len(sql_pattern_scores), 1),
            ilike_compliance=ilike_compliance,
            # Layer C
            e2e_pass_rate=_pass_rate(er),
            avg_iterations=_avg(er, "iterations_used"),
            avg_relevance=_avg(er, "relevance_score"),
            avg_overall_score=_avg(er, "overall_score"),
            # Raw
            intent_results=ir,
            sql_results=sr,
            e2e_results=er,
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — PRETTY REPORTER
# ══════════════════════════════════════════════════════════════════════════════

def print_report(report: EvalReport) -> None:
    """Print a structured, human-readable evaluation report to stdout."""

    SEP  = "═" * 62
    SEP2 = "─" * 62

    print(f"\n{SEP}")
    print(f"  FUNCTIONAL EVALUATION REPORT")
    print(f"  {report.timestamp}  |  mode={report.mode.upper()}")
    print(f"{SEP}")

    # ── Layer A ────────────────────────────────────────────────────────────
    if report.intent_results:
        print(f"\n  LAYER A — Intent Extraction  ({len(report.intent_results)} cases)")
        print(f"  {SEP2}")
        print(f"  Pass rate       : {report.intent_pass_rate:.0%}")
        print(f"  Entity recall   : {report.avg_entity_recall:.2f}")
        print(f"  Metric recall   : {report.avg_metric_recall:.2f}")
        print(f"  F1 (entities)   : {report.avg_f1_entity:.2f}")
        print()
        for r in report.intent_results:
            status = "✓" if r.passed else "✗"
            print(f"  {status} {r.test_id:<10} recall={r.entity_recall:.2f}  metric={r.metric_recall:.2f}  f1={r.f1_entity:.2f}  [{r.duration_ms}ms]")
            for issue in r.issues:
                print(f"       ↳ {issue}")

    # ── Layer B ────────────────────────────────────────────────────────────
    if report.sql_results:
        print(f"\n  LAYER B — SQL Quality  ({len(report.sql_results)} cases)")
        print(f"  {SEP2}")
        print(f"  Pass rate       : {report.sql_pass_rate:.0%}")
        print(f"  Pattern score   : {report.avg_pattern_score:.2f}")
        print(f"  ILIKE compliance: {report.ilike_compliance:.0%}")
        print()
        for r in report.sql_results:
            status = "✓" if r.passed else "✗"
            entity_ok = f"entity={r.entity_completeness_score:.2f}"
            print(f"  {status} {r.test_id:<10} {entity_ok}  select_only={r.is_select_only}  groupby_ok={r.has_group_by_when_needed}  [{r.duration_ms}ms]")
            for issue in r.issues:
                print(f"       ↳ {issue}")

    # ── Layer C ────────────────────────────────────────────────────────────
    if report.e2e_results:
        print(f"\n  LAYER C — End-to-End ReAct Loop  ({len(report.e2e_results)} cases)")
        print(f"  {SEP2}")
        print(f"  Pass rate       : {report.e2e_pass_rate:.0%}")
        print(f"  Avg iterations  : {report.avg_iterations:.1f}")
        print(f"  Avg relevance   : {report.avg_relevance:.2f}  (-1 = unavailable)")
        print(f"  Avg score       : {report.avg_overall_score:.2f}")
        print()
        for r in report.e2e_results:
            status = "✓" if r.passed else "✗"
            print(
                f"  {status} {r.test_id:<10} "
                f"score={r.overall_score:.2f}  "
                f"iters={r.iterations_used}  "
                f"rows={r.row_count}  "
                f"relevance={r.relevance_score:.2f}  "
                f"[{r.duration_ms}ms]"
            )
            for issue in r.issues:
                print(f"       ↳ {issue}")

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print(f"  SUMMARY")
    print(f"  {SEP2}")
    if report.intent_results:
        _bar("Intent pass rate ", report.intent_pass_rate)
    if report.sql_results:
        _bar("SQL pass rate    ", report.sql_pass_rate)
        _bar("ILIKE compliance ", report.ilike_compliance)
    if report.e2e_results:
        _bar("E2E pass rate    ", report.e2e_pass_rate)
        _bar("Avg overall score", report.avg_overall_score)
    print(f"{SEP}\n")


def save_report_json(report: EvalReport, path: str = "project_eval_report.json") -> None:
    """Serialize the full report to JSON for downstream analysis."""

    def _to_dict(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _to_dict(v) for k, v in obj.__dict__.items()}
        if isinstance(obj, list):
            return [_to_dict(i) for i in obj]
        if isinstance(obj, dict):
            return {k: _to_dict(v) for k, v in obj.items()}
        return obj

    with open(path, "w") as f:
        json.dump(_to_dict(report), f, indent=2, default=str)
    print(f"[Report] Saved to {path}")


def _print_layer_result(layer: str, passed: bool, issues: list[str], extra: str = "") -> None:
    status = "✓ PASS" if passed else "✗ FAIL"
    extra_str = f"  {extra}" if extra else ""
    print(f"  [{layer}] {status}{extra_str}")
    for issue in issues[:3]:   # cap at 3 lines per case in live output
        print(f"        → {issue}")


def _bar(label: str, value: float, width: int = 20) -> None:
    filled = int(value * width)
    bar    = "█" * filled + "░" * (width - filled)
    print(f"  {label}: [{bar}]  {value:.0%}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — CONFIG & ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

# ── Edit these directly if not using env vars ──────────────────────────────
CONFIG = {
    # OpenAI / Azure key
    "openai_api_key": os.getenv("OPENAI_API_KEY",""),

    # Model to use for agent calls during eval
    # Use gpt-4.1-mini for cheaper intent-only runs; gpt-4.1 for full eval
    "model": os.getenv("EVAL_MODEL", "gpt-4.1"),

    # PostgreSQL connection string — required for full / sql modes
    # e.g. "postgresql://user:pass@localhost:5432/realestate"
    "database_url": os.getenv("DATABASE_URL", "postgresql://AkashAtSigma:EbRoot_sigma6@localhost:5432/pipeline_one_db1_db2"),

    # LangSmith (optional)
    "langsmith_api_key":  os.getenv("LANGSMITH_API_KEY", ""),
    "langsmith_project":  os.getenv("LANGSMITH_PROJECT", "project-sql-eval"),

    # Output paths
    "report_json": "project_eval_report.json",
}


def _build_db_executor(database_url: str) -> Callable[[str], list[dict]]:
    """
    Build a real PostgreSQL executor from a connection string.
    Requires: pip install psycopg2-binary
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise RuntimeError(
            "psycopg2-binary not installed. "
            "Run: pip install psycopg2-binary\n"
            "Or set mode=sql/intent to skip DB execution."
        )

    def executor(sql: str) -> list[dict]:
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    return executor


def main():
    parser = argparse.ArgumentParser(
        description="Functional evaluation for project SQL agent"
    )
    parser.add_argument(
        "--mode",
        choices=["intent", "sql", "full"],
        default="intent",
        help=(
            "intent  = Layer A only (no DB needed)\n"
            "sql     = Layer A + B  (mock DB)\n"
            "full    = All layers   (requires DATABASE_URL)"
        ),
    )
    parser.add_argument(
        "--langsmith",
        action="store_true",
        help="Also push results to LangSmith (requires LANGSMITH_API_KEY)",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        help="Run only specific test case IDs, e.g. --cases PC_001 PC_002",
    )
    parser.add_argument(
        "--save",
        default=CONFIG["report_json"],
        help="Path to save JSON report (default: project_eval_report.json)",
    )
    args = parser.parse_args()

    # ── Validate config ────────────────────────────────────────────────────
    if "YOUR-KEY-HERE" in CONFIG["openai_api_key"]:
        print("[ERROR] Set OPENAI_API_KEY env var or edit CONFIG in this file.")
        sys.exit(1)

    if args.mode == "full" and not CONFIG["database_url"]:
        print("[ERROR] --mode full requires DATABASE_URL env var.")
        sys.exit(1)

    # ── Build dependencies ─────────────────────────────────────────────────
    openai_client = OpenAI(api_key=CONFIG["openai_api_key"])

    db_executor = None
    if args.mode == "full":
        db_executor = _build_db_executor(CONFIG["database_url"])
    # sql mode uses mock executor (default in EvalRunner)

    # ── Filter test cases ──────────────────────────────────────────────────
    cases = TEST_CASES
    if args.cases:
        cases = [tc for tc in TEST_CASES if tc["id"] in args.cases]
        if not cases:
            print(f"[ERROR] No matching cases for IDs: {args.cases}")
            sys.exit(1)
        print(f"[INFO] Running {len(cases)} selected case(s): {[tc['id'] for tc in cases]}")

    # ── LangSmith ──────────────────────────────────────────────────────────
    if args.langsmith:
        if not HAS_LANGSMITH:
            print("[ERROR] pip install langsmith to use --langsmith flag.")
            sys.exit(1)
        if not CONFIG["langsmith_api_key"]:
            print("[ERROR] LANGSMITH_API_KEY not set.")
            sys.exit(1)
        os.environ["LANGCHAIN_API_KEY"]  = CONFIG["langsmith_api_key"]
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_PROJECT"]  = CONFIG["langsmith_project"]

        ls_eval = LangSmithEvaluator(
            openai_client=openai_client,
            db_executor=db_executor or EvalRunner._mock_executor,
            model=CONFIG["model"],
        )
        ls_eval.run()
        # LangSmith run is separate from local report; continue to local eval below

    # ── Local evaluation ────────────────────────────────────────────────────
    runner = EvalRunner(
        openai_client=openai_client,
        db_executor=db_executor,
        model=CONFIG["model"],
        mode=args.mode,
    )
    report = runner.run(test_cases=cases)
    print_report(report)
    save_report_json(report, path=args.save)


if __name__ == "__main__":
    main()
