"""
ReAct Query Builder Agent  —  v3.0
====================================
Clean architecture: User Query + Schema → SQL. Nothing else pre-filters the intent.

The previous design passed tool_selections and plan steps into the SQL builder,
which caused the builder to only filter on the first entity it saw (e.g. Hinjewadi)
and silently discard the rest (e.g. Baner). That pre-filtering layer is gone.

New pipeline
─────────────
  User Query (raw string)
       │
       ▼
  [1] IntentExtractor      — NL → structured intent dict
       │                     reads: user_query + schema
       │                     writes: analysis_type, metrics, entities (ALL of them),
       │                             filters, group_by, order_by, time_series
       ▼
  [2] TransactionQueryBuilder.run(intent)
       │
       ├── BUILD    — schema-grounded SQL; ALL intent entities in WHERE clause
       ├── REVIEW   — pre-execution gate; entity completeness check is #1 priority
       ├── EXECUTE  — PostgreSQL via db_executor
       ├── OBSERVE  — checks result for missing entities, wrong column, wrong gran
       └── REFLECT  — LLM-driven column fallback (schema as ground truth; no hardcoded map)
           └── REWRITE → back to EXECUTE …  (up to MAX_ITERATIONS)

Top-level entry point
──────────────────────
  result = run_query(user_query, client, db_executor)

What was removed vs v2
───────────────────────
  • tool_selections parameter   — caused single-entity filtering
  • plan steps injection        — narrowed scope before SQL was built
  • COLUMN_ALIAS_MAP            — replaced by LLM schema reasoning
  • registry dependency         — not needed without tool selection layer
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from openai import OpenAI

from utils.data_retrieval.semantic_different_categories import resolve_intent_category_filters
from agents.data_retrieval_transaction.constants import MAX_ITERATIONS, REVIEW_SAMPLE
from agents.data_retrieval_transaction.helpers import (
    clean_sql,
    extract_filter_columns,
    parse_json,
    validate_select_only,
)
from agents.data_retrieval_transaction.intent_extractor import IntentExtractor
from agents.data_retrieval_transaction.models import (
    Iteration,
    ObserveVerdict,
    QueryResult,
    StepStatus,
)
from agents.data_retrieval_transaction.prompts import (
    SQL_BUILD_PROMPT,
    SQL_FIX_PROMPT,
    SQL_OBSERVE_PROMPT,
    SQL_PROBE_PROMPT,
    SQL_REFLECT_PROMPT,
    SQL_REVIEW_PROMPT,
)
from agents.data_retrieval_transaction.schema import TRANSACTION_QUERY_SCHEMA

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Transaction Query Builder  (ReAct loop)
# ══════════════════════════════════════════════════════════════════════════════

class TransactionQueryBuilder:
    """
    Production-grade ReAct SQL query builder.

    Takes a structured intent dict (from IntentExtractor) and runs a
    BUILD → REVIEW → EXECUTE → OBSERVE → REFLECT loop until a satisfactory
    result is produced or MAX_ITERATIONS is reached.

    No tool registry. No plan steps. No pre-filtering.
    All entities in the intent appear in every generated SQL.

    Usage
    ─────
        extractor = IntentExtractor(client)
        intent    = extractor.extract(user_query)

        builder   = TransactionQueryBuilder(client=client, db_executor=run_sql)
        result    = builder.run(intent)

        result.sql        → final SQL string
        result.rows       → result rows  (list[dict])
        result.intent     → extracted intent (for audit/logging)
        result.trace      → full ReAct trace  (list[Iteration])
        result.success    → True if GOOD verdict reached
    """

    def __init__(
        self,
        client:         OpenAI,
        db_executor:    Callable[[str], list[dict]] | None = None,
        model:          str = "gpt-5.1",
        max_iterations: int = MAX_ITERATIONS,
        **kwargs,
    ) -> None:
        self.client         = client
        self.db_executor    = db_executor
        self.model          = model
        self.max_iterations = max_iterations

        # Per-run state — reset on each run()
        self.trace:       list[Iteration] = []
        self.last_usage:  Any             = None
        self.total_usage: Any             = None
        self._fix_usages: list            = []

    # ══════════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════════

    def run(self, intent: dict) -> QueryResult:
        """
        Run the full ReAct loop and return a QueryResult.

        Loop:
          BUILD / REWRITE → REVIEW → EXECUTE → OBSERVE
                                                  │
                                             GOOD → return
                                                  │
                                             REFLECT → new SQL → next iteration
        """
        self.trace       = []
        self._fix_usages = []

        if intent.get("route") == "clarify" or intent.get("needs_clarification"):
            return QueryResult(
                sql="",
                rows=[],
                intent=intent,
                iterations=0,
                trace=[],
                success=False,
                error="Intent clarification required.",
            )

        # ── STEP 0A: SEMANTIC CATEGORY RESOLUTION ─────────────────────────────
        # Convert user wording ("2BHK", "residential", "mrkt") to exact DB
        # values for category-like columns before SQL generation.
        self._resolve_semantic_category_filters(intent)

        # ── STEP 0: PROBE ─────────────────────────────────────────────────────
        # Identify which columns actually contain the entities to avoid empty results.
        probe_data = self._probe(intent)
        print(f"[ReAct] PROBE results: {probe_data}")

        current_sql: str | None = None
        last_rows:   list[dict] = []
        last_error:  str | None = None

        for i in range(self.max_iterations):
            iteration = Iteration(index=i, sql="", status=StepStatus.BUILD)
            t0 = time.monotonic()

            # ── STEP 1: BUILD / REWRITE ───────────────────────────────────────
            try:
                if current_sql is None:
                    print(f"\n[ReAct iter={i}] BUILD — generating initial SQL...")
                    logger.info("[iter=%d] BUILD — generating SQL from intent", i)
                    current_sql      = self._build(intent, probe_results=probe_data)
                    iteration.status = StepStatus.BUILD
                    print(f"SQL GENERATED:\n{current_sql}\n")
                else:
                    print(f"\n[ReAct iter={i}] REWRITE — applying correction...")
                    logger.info("[iter=%d] REWRITE — applying reflected SQL", i)
                    iteration.status = StepStatus.REWRITE
                
                iteration.usage = self.last_usage
                iteration.sql   = current_sql

            except Exception as exc:
                iteration.error  = str(exc)
                iteration.status = StepStatus.FAILED
                self.trace.append(iteration)
                logger.error("[iter=%d] BUILD failed: %s", i, exc)
                break

            # ── STEP 2: REVIEW ────────────────────────────────────────────────
            print(f"[ReAct iter={i}] REVIEW — checking entity completeness...")
            logger.info("[iter=%d] REVIEW — pre-execution gate", i)
            review           = self._review(intent, current_sql)
            iteration.review = review
            iteration.status = StepStatus.REVIEW

            if not review.get("approved", True):
                missing = (
                    review
                    .get("entity_completeness_check", {})
                    .get("missing", [])
                )
                print(f"[ReAct iter={i}] REVIEW rejected — missing entities: {missing}")
                logger.info(
                    "[iter=%d] REVIEW rejected  confidence=%s  missing=%s  reason=%s",
                    i,
                    review.get("confidence"),
                    missing,
                    review.get("reasoning"),
                )
                fix_sql = (review.get("suggested_fix") or "").strip()
                if fix_sql.lower().startswith(("select", "with")):
                    current_sql      = clean_sql(fix_sql)
                    iteration.sql    = current_sql
                    print(f"[ReAct iter={i}] REVIEW — applied suggested fix.")
                    logger.info("[iter=%d] REVIEW — applying reviewer's corrected SQL", i)

            # ── STEP 3: EXECUTE ───────────────────────────────────────────────
            logger.info("[iter=%d] EXECUTE — %s", i, current_sql)
            rows, db_error = self._execute(current_sql)
            last_rows      = rows
            last_error     = db_error

            iteration.rows          = rows
            iteration.error         = db_error
            iteration.status        = StepStatus.EXECUTE
            iteration.columns_tried = extract_filter_columns(current_sql)

            # ── STEP 4: OBSERVE ───────────────────────────────────────────────
            print(f"[ReAct iter={i}] OBSERVE — rows={len(rows)} error={db_error or 'none'}")
            logger.info(
                "[iter=%d] OBSERVE — rows=%d  error=%s",
                i, len(rows), db_error or "none",
            )
            observation       = self._observe(intent, current_sql, rows, db_error)
            
            # Normalize verdict for robustness
            raw_v = str(observation.get("verdict", "good")).lower().strip()
            if raw_v in ["good", "success", "correct", "perfect", "satisfactory"]:
                v_enum = ObserveVerdict.GOOD
            elif "empty" in raw_v:
                v_enum = ObserveVerdict.EMPTY
            elif "error" in raw_v:
                v_enum = ObserveVerdict.DB_ERROR
            else:
                try:
                    v_enum = ObserveVerdict(raw_v)
                except ValueError:
                    v_enum = ObserveVerdict.IRRELEVANT

            iteration.verdict = v_enum
            iteration.reflect = observation
            iteration.status  = StepStatus.OBSERVE
            iteration.duration_ms = int((time.monotonic() - t0) * 1000)
            self.trace.append(iteration)
            
            print(f"[ReAct iter={i}] VERDICT: {iteration.verdict.value}")
            print(f"REASON: {observation.get('reason')}")
            
            verdict = iteration.verdict

            # ── DONE ──────────────────────────────────────────────────────────
            if verdict == ObserveVerdict.GOOD:
                logger.info("[iter=%d] DONE — verdict=GOOD", i)
                return QueryResult(
                    sql=current_sql,
                    rows=rows,
                    intent=intent,
                    iterations=i + 1,
                    trace=self.trace,
                    success=True,
                    usage=self.total_usage,
                )

            # ── STEP 5: REFLECT → REWRITE ─────────────────────────────────────
            if i < self.max_iterations - 1:
                logger.info(
                    "[iter=%d] REFLECT — verdict=%s  missing=%s  suspect=%s",
                    i,
                    verdict,
                    observation.get("missing_entities", []),
                    observation.get("column_suspect"),
                )
                print(f"[ReAct iter={i}] REFLECT — diagnosing and correcting...")
                history    = self._build_history_summary()
                reflection = self._reflect(intent, current_sql, observation, history)
                
                if reflection.get("root_cause"):
                    print(f"REASONING: {reflection.get('root_cause')}")
                if reflection.get("explanation"):
                    print(f"EXPLANATION: {reflection.get('explanation')}")

                corrected = (reflection.get("corrected_sql") or "").strip()
                if corrected.lower().startswith(("select", "with")):
                    current_sql = clean_sql(corrected)
                    print(f"CORRECTED SQL:\n{current_sql}\n")
                    print(f"[ReAct iter={i}] REWRITE — action: {reflection.get('action')}")
                    logger.info(
                        "[iter=%d] REWRITE — action=%s  missing_fix=%s  new_col=%s",
                        i,
                        reflection.get("action"),
                        reflection.get("missing_entities_fix", []),
                        reflection.get("new_column"),
                    )
                elif verdict == ObserveVerdict.DB_ERROR and db_error:
                    current_sql = self._fix_sql(current_sql, db_error)
                    print(f"[ReAct iter={i}] REWRITE — applied hard fallback fix.")
                    logger.info("[iter=%d] REWRITE — via hard fix() fallback", i)
                else:
                    logger.warning(
                        "[iter=%d] REFLECT produced no valid SQL — stopping early", i
                    )
                    break
            else:
                logger.warning(
                    "Max iterations (%d) reached without a satisfactory result",
                    self.max_iterations,
                )

        # ── Best-effort fallback ───────────────────────────────────────────────
        best = self._best_effort(last_rows)
        return QueryResult(
            sql=current_sql or "",
            rows=best,
            intent=intent,
            iterations=self.max_iterations,
            trace=self.trace,
            success=False,
            usage=self.total_usage,
            error=f"Max iterations ({self.max_iterations}) reached without GOOD verdict.",
        )

    # ── Backward compatibility ─────────────────────────────────────────────────

    def build(self, intent: dict, **kwargs) -> str:
        """Returns SQL string only — no ReAct loop."""
        if intent.get("route") == "clarify" or intent.get("needs_clarification"):
            return ""
        return self._build(intent)

    def fix(self, sql: str, error: str) -> str:
        """Fix a failed SQL string."""
        return self._fix_sql(sql, error)

    def pop_fix_usages(self) -> list:
        out = self._fix_usages[:]
        self._fix_usages = []
        return out

    def _resolve_semantic_category_filters(self, intent: dict) -> None:
        """Enrich intent with exact DB values for semantic category filters."""
        if self.db_executor is None:
            intent.setdefault("semantic_resolved_filters", {})
            return

        try:
            resolved = resolve_intent_category_filters(
                intent=intent,
                client=self.client,
                db_executor=self.db_executor,
                model=self.model,
            )
            if resolved:
                print(f"[ReAct] SEMANTIC category filters: {resolved}")
                logger.info("Semantic category filters resolved: %s", resolved)
            else:
                logger.info("No semantic category filters resolved.")
        except Exception as exc:
            # Semantic resolution improves precision, but the ReAct loop can still
            # continue with the raw intent if this auxiliary step fails.
            intent.setdefault("semantic_resolved_filters", {})
            logger.warning("Semantic category resolution failed: %s", exc)

    # ══════════════════════════════════════════════════════════════════════════
    # Private — LLM calls
    # ══════════════════════════════════════════════════════════════════════════

    def _build(self, intent: dict, probe_results: str = "No probe data") -> str:
        prompt = SQL_BUILD_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            intent_json=json.dumps(intent, indent=2),
            probe_results=probe_results,
        )
        response = self._chat(
            system=(
                "You generate safe, valid, schema-grounded PostgreSQL SELECT queries. "
                "Use the provided PROBE RESULTS to pick columns that actually contain data. "
                "Include EVERY entity from the intent in the WHERE clause using OR. "
                "Return only the SQL — no markdown, no explanation."
            ),
            user=prompt,
        )
        self.last_usage = response.usage
        raw = response.choices[0].message.content.strip()
        return validate_select_only(clean_sql(raw))

    def _probe(self, intent: dict) -> str:
        """Run a discovery query to find where entities reside."""
        locations = (intent.get("entities") or {}).get("locations") or []
        projects  = (intent.get("entities") or {}).get("projects") or []
        space_filters = (intent.get("entities") or {}).get("space_filters") or {}
        
        # Check if space_filters has any non-empty values
        from agents.data_retrieval_transaction.helpers import contains_space_value
        if not locations and not projects and not contains_space_value(space_filters):
            return "No location/project entities to probe."

        prompt = SQL_PROBE_PROMPT.format(
            intent_json=json.dumps(intent, indent=2),
        )
        response = self._chat(
            system="You generate PostgreSQL discovery queries. Return only SQL.",
            user=prompt,
        )
        sql = clean_sql(response.choices[0].message.content.strip())
        print(f"[ReAct] PROBE SQL: {sql}")
        
        try:
            rows = self.db_executor(sql)
            if not rows:
                return "Probe returned no rows."
            return json.dumps(rows, indent=2)
        except Exception as e:
            logger.warning("Probe failed: %s", e)
            return f"Probe failed: {e}"

    def _review(self, intent: dict, sql: str) -> dict:
        prompt = SQL_REVIEW_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            intent_json=json.dumps(intent, indent=2),
            sql=sql,
        )
        response = self._chat(
            system=(
                "You review PostgreSQL queries for correctness and entity completeness. "
                "Respond only with JSON."
            ),
            user=prompt,
        )
        return parse_json(
            response.choices[0].message.content,
            default={
                "approved": True,
                "confidence": 50,
                "issues": [],
                "entity_completeness_check": {},
                "reasoning": "parse error — defaulting approved",
            },
        )

    def _observe(
        self,
        intent:   dict,
        sql:      str,
        rows:     list[dict],
        db_error: str | None,
    ) -> dict:
        sample = rows[:REVIEW_SAMPLE] if rows else []
        prompt = SQL_OBSERVE_PROMPT.format(
            intent_json=json.dumps(intent, indent=2),
            sql=sql,
            row_count=len(rows),
            db_error=db_error or "none",
            sample_rows=json.dumps(sample, indent=2, default=str),
        )
        response = self._chat(
            system=(
                "You evaluate SQL query results for correctness, entity completeness, "
                "and intent alignment. Respond only with JSON."
            ),
            user=prompt,
        )
        return parse_json(
            response.choices[0].message.content,
            default={
                "verdict": "good",
                "confidence": 50,
                "reason": "parse error — defaulting to good",
                "missing_entities": [],
            },
        )

    def _reflect(
        self,
        intent:      dict,
        sql:         str,
        observation: dict,
        history:     str,
    ) -> dict:
        sample_rows: list[dict] = []
        if self.trace:
            sample_rows = self.trace[-1].rows[:REVIEW_SAMPLE]

        prompt = SQL_REFLECT_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            intent_json=json.dumps(intent, indent=2),
            sql=sql,
            observation=json.dumps(observation, indent=2),
            history=history,
            sample_rows=json.dumps(sample_rows, indent=2, default=str),
        )
        response = self._chat(
            system=(
                "You diagnose SQL failures and produce corrected SQL. "
                "Use schema as ground truth. Ensure ALL intent entities appear "
                "in the corrected WHERE clause. Respond only with JSON."
            ),
            user=prompt,
        )
        return parse_json(response.choices[0].message.content, default={})

    def _fix_sql(self, sql: str, error: str) -> str:
        """Hard fallback for DB errors — single-purpose dedicated fixer."""
        prompt = SQL_FIX_PROMPT.format(
            sql=sql,
            error=error,
            schema=TRANSACTION_QUERY_SCHEMA,
        )
        response = self._chat(
            system="You fix PostgreSQL SELECT queries. Return only corrected SQL.",
            user=prompt,
        )
        self._fix_usages.append(response.usage)
        fixed = clean_sql(response.choices[0].message.content.strip())
        return validate_select_only(fixed)

    # ══════════════════════════════════════════════════════════════════════════
    # Private — execution
    # ══════════════════════════════════════════════════════════════════════════

    def _execute(self, sql: str) -> tuple[list[dict], str | None]:
        """
        Run SQL via the injected db_executor.
        Never raises — exceptions become the error string.
        """
        try:
            rows = self.db_executor(sql)
            return rows or [], None
        except Exception as exc:
            logger.warning("DB execution error: %s", exc)
            return [], str(exc)

    # ══════════════════════════════════════════════════════════════════════════
    # Private — helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _chat(self, system: str, user: str):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            timeout=30,
        )
        self.last_usage = response.usage
        self._update_total_usage(response.usage)
        return response

    def _update_total_usage(self, usage: Any):
        if usage is None: return
        if self.total_usage is None:
            # We can't easily instantiate a CompletionUsage, so we'll just 
            # keep it as a dict or a simple object for summing.
            self.total_usage = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens
            }
        else:
            self.total_usage["prompt_tokens"]     += usage.prompt_tokens
            self.total_usage["completion_tokens"] += usage.completion_tokens
            self.total_usage["total_tokens"]      += usage.total_tokens

    def _build_history_summary(self) -> str:
        """
        Compact history for the reflection prompt.
        Surfaces columns_tried_in_filters so the LLM avoids retrying
        columns that already returned 0 rows.
        """
        summary = []
        for it in self.trace:
            summary.append({
                "iter":                     it.index,
                "sql_snippet":              it.sql[:400] + ("…" if len(it.sql) > 400 else ""),
                "verdict":                  it.verdict,
                "error":                    it.error,
                "row_count":                len(it.rows),
                "columns_tried_in_filters": it.columns_tried,
                "missing_entities":         it.reflect.get("missing_entities", []),
                "observe_reason":           it.reflect.get("reason"),
                "column_suspect":           it.reflect.get("column_suspect"),
                "suggested_replacements":   it.reflect.get("suggested_replacement_columns"),
            })
        return json.dumps(summary, indent=2, default=str)

    def _best_effort(self, last_rows: list[dict]) -> list[dict]:
        """Return last non-empty result from trace after exhausting iterations."""
        for it in reversed(self.trace):
            if it.rows:
                return it.rows
        return last_rows


# ══════════════════════════════════════════════════════════════════════════════
# Top-level convenience function
# ══════════════════════════════════════════════════════════════════════════════

def run_query(
    user_query:  str,
    client:      OpenAI,
    db_executor: Callable[[str], list[dict]],
    model:       str = "gpt-5.1",
) -> QueryResult:
    """
    Single entry point: raw user query string → QueryResult.

    Pipeline:
      1. IntentExtractor converts the query to a structured intent dict.
      2. TransactionQueryBuilder runs the ReAct loop against the intent.

    Example
    ───────
        result = run_query(
            user_query  = "total sales units sold and rate per sqft "
                          "compare baner and hinjewadi",
            client      = openai_client,
            db_executor = run_sql,
        )
        print(result.sql)
        print(result.rows)
        print(result.success)
    """
    extractor = IntentExtractor(client=client, model=model)
    intent    = extractor.extract(user_query)

    builder   = TransactionQueryBuilder(
        client=client,
        db_executor=db_executor,
        model=model,
    )
    return builder.run(intent)
