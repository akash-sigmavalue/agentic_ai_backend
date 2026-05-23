import json

from utils.data_retrieval.clarification import (
    SPACE_CLARIFICATION_OPTIONS,
    SPACE_CLARIFICATION_QUESTION,
)
from utils.data_retrieval.query_executor import ExecutionEngine
from agents.data_retrieval_transaction.intent_extractor import IntentExtractor
from agents.data_retrieval_transaction.query_builder import TransactionQueryBuilder


class TransactionDomainAgent:
    def __init__(self, client):
        self.domain_key = "transaction"
        self.display_name = "Transaction Agent"
        
        # Use gpt-4o-mini for intent extraction (cost efficient)
        self.intent_extractor = IntentExtractor(client, model="gpt-4o-mini")
        
        # Use gpt-5.1 for the core ReAct loop (high reasoning)
        self.query_builder = TransactionQueryBuilder(client=client, model="gpt-5.1")
        
        self.executor = ExecutionEngine(self.query_builder)
        self.query_builder.db_executor = self._db_executor

    def _db_executor(self, sql: str) -> list[dict]:
        """Bridge for TransactionQueryBuilder to run SQL via ExecutionEngine."""
        res = self.executor.execute(sql)
        if res["status"] == "success":
            return res["data"]
        raise Exception(res.get("error", "Database execution failed"))

    def _event(self, event_type: str, content, **kwargs) -> dict:
        payload = {"type": event_type, "content": content}
        payload.update(kwargs)
        return payload

    def _build_result_payload(self, db_result: dict) -> dict:
        return {
            "domain": self.domain_key,
            "title": f"{self.display_name} Data",
            "columns": db_result.get("columns", []),
            "rows": db_result.get("data", []),
            "row_count": db_result.get("row_count", 0),
        }

    def _format_algorithm_output(self, intent: dict, sql: str | None, db_result: dict) -> str:
        entities = intent.get("entities") or {}
        metrics = [
            metric.get("alias") or metric.get("name")
            for metric in (intent.get("metrics") or [])
            if metric.get("alias") or metric.get("name")
        ]
        locations = [loc.get("value") for loc in (entities.get("locations") or []) if loc.get("value")]
        space_filters = {
            key: value
            for key, value in (entities.get("space_filters") or {}).items()
            if value not in (None, "", [])
        }
        category_filters = {
            key: value
            for key, value in (entities.get("category_filters") or {}).items()
            if value not in (None, "", [])
        }
        group_by = intent.get("group_by") or []
        order_by = intent.get("order_by") or []
        row_count = db_result.get("row_count", 0)

        return "\n".join(
            [
                "Interpreted intent:",
                f"- Analysis type: {intent.get('analysis_type') or 'lookup'}",
                f"- Metrics: {', '.join(metrics) if metrics else 'schema-backed lookup fields'}",
                f"- Entities: {', '.join(locations) if locations else 'none explicitly detected'}",
                "",
                "Algorithm followed:",
                "1. Read the user query against the available transaction schema.",
                "2. Selected only schema-backed columns for entities, filters, and metrics.",
                f"3. Applied space filters: {json.dumps(space_filters, default=str) if space_filters else 'none'}.",
                f"4. Applied category filters: {json.dumps(category_filters, default=str) if category_filters else 'none'}.",
                f"5. Used grouping: {json.dumps(group_by, default=str) if group_by else 'none'} and ordering: {json.dumps(order_by, default=str) if order_by else 'none'}.",
                "6. Generated and reviewed a safe SELECT query before execution.",
                f"7. Executed the query and returned {row_count} matching row(s).",
                "",
                "SQL used:",
                sql or "No SQL was generated.",
                "",
                "Result:",
            ]
        )

    def execute_events(self, question: str):
        try:
            yield self._event("stage", f"{self.display_name} · Stage 1: Extracting intent and entities...")
            intent = self.intent_extractor.extract(question)
            print(f"\n{'='*80}")
            print(f"EXTRACTED INTENT ({self.display_name}):")
            print(f"{'='*80}")
            print(json.dumps(intent, indent=2))
            print(f"{'='*80}\n", flush=True)
            if self.intent_extractor.last_usage is not None:
                yield self._event(
                    "token_usage_raw",
                    None,
                    stage_name=f"{self.domain_key}.s1_intent",
                    usage=self.intent_extractor.last_usage,
                )
            yield self._event("intent", intent, agent=self.domain_key)
            yield self._event(
                "debug_trace",
                {
                    "phase": "observe",
                    "step": "intent_extraction",
                    "summary": "Intent extracted via V3 IntentExtractor.",
                    "analysis_type": intent.get("analysis_type"),
                    "metrics": [m.get("alias") for m in (intent.get("metrics") or [])],
                    "locations": [loc.get("value") for loc in (intent.get("entities") or {}).get("locations") or []],
                },
                agent=self.domain_key,
            )
        except Exception as error:
            yield self._event("error", f"{self.display_name} Stage 1 failed: {str(error)}")
            return {
                "status": "error",
                "domain": self.domain_key,
                "error": str(error),
                "report_text": "",
                "report_chunks": [],
            }

        route = (intent.get("route") or "internal_db").lower()
        if route == "clarify" or intent.get("needs_clarification"):
            questions = intent.get("clarification_questions") or [
                "Please specify the unit, building, parcel / survey / CTS / khasra / plot number, project, location, micromarket, city, state, or country."
            ]
            clarification_payload = {
                "message": intent.get("clarification_reason") or "I need a little more detail to answer safely.",
                "questions": questions,
            }
            if SPACE_CLARIFICATION_QUESTION in questions:
                clarification_payload["clarification_type"] = "space_filter"
                clarification_payload["options"] = SPACE_CLARIFICATION_OPTIONS
            return {
                "status": "clarify",
                "domain": self.domain_key,
                "route": route,
                "intent": intent,
                "clarification_payload": clarification_payload,
                "report_text": "",
                "report_chunks": [],
            }

        # ReAct loop initialization
        plan = {"steps": ["ReAct SQL Loop"], "explanation": "v3.0 integrated ReAct pipeline"}
        tool_selections = []
        report_chunks = []
        sql = None

        yield self._event("stage", f"{self.display_name} · Running ReAct SQL loop...")
        try:
            # The v3 QueryBuilder handles planning, execution, observation, and reflection internally.
            result = self.query_builder.run(intent)
            sql = result.sql
            db_result = {
                "status": "success" if result.success else "error",
                "data": result.rows,
                "columns": list(result.rows[0].keys()) if result.rows else [],
                "row_count": len(result.rows),
                "error": result.error,
                "iterations": result.iterations,
            }

            if result.usage is not None:
                yield self._event(
                    "token_usage_raw",
                    None,
                    stage_name=f"{self.domain_key}.v3_react_loop",
                    usage=result.usage,
                )
            
            if sql:
                yield self._event("sql_query", sql, agent=self.domain_key)
            
            # Yield detailed trace for the UI to show 'under the hood' working
            for it in result.trace:
                 yield self._event("debug_trace", {
                     "phase": "react_loop",
                     "step": f"Iteration {it.index + 1}: {it.status.value}",
                     "verdict": it.verdict.value if it.verdict else None,
                     "sql": it.sql,
                     "error": it.error,
                     "row_count": len(it.rows),
                     "observation": it.reflect.get("reason"),
                     "action": it.reflect.get("action"),
                     "duration_ms": it.duration_ms
                 }, agent=self.domain_key)
            
            if not result.success and result.error:
                 yield self._event("error", f"{self.display_name} ReAct loop failed: {result.error}")

        except Exception as error:
            yield self._event("error", f"{self.display_name} SQL pipeline failed: {str(error)}")
            return {
                "status": "error",
                "domain": self.domain_key,
                "error": str(error),
                "report_text": "",
                "report_chunks": [],
            }

        if db_result["status"] == "success":
            yield self._event(
                "observation_preview",
                f"{self.display_name}: retrieved {db_result['row_count']} rows.",
                data=db_result["data"][:3],
                agent=self.domain_key,
            )
            yield self._event(
                "debug_trace",
                {
                    "phase": "observe",
                    "step": "database_fetch",
                    "summary": "SQL query executed successfully and rows were fetched via ReAct loop.",
                    "row_count": db_result.get("row_count", 0),
                    "iterations": db_result.get("iterations"),
                },
                agent=self.domain_key,
            )
            yield self._event("result_set", self._build_result_payload(db_result), agent=self.domain_key)
            algorithm_output = self._format_algorithm_output(intent, sql, db_result)
            report_chunks.append(algorithm_output)
            yield self._event("report_chunk", algorithm_output + "\n", agent=self.domain_key)
            raw_output = json.dumps(db_result["data"], indent=2, default=str)
            report_chunks.append(raw_output)
            yield self._event("report_chunk", raw_output + "\n", agent=self.domain_key)
        else:
            error_output = db_result.get("error") or "Database query failed."
            report_chunks.append(error_output)
            yield self._event("report_chunk", error_output + "\n", agent=self.domain_key)

        return {
            "status": "success",
            "domain": self.domain_key,
            "route": route,
            "intent": intent,
            "plan": plan,
            "tool_selections": tool_selections,
            "sql": sql,
            "db_result": db_result,
            "needs_live_web": False,
            "report_text": "\n".join(report_chunks).strip(),
            "report_chunks": report_chunks,
        }
