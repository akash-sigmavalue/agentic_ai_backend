from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from agents.data_retrieval_portfolio.query_builder import PortfolioQueryBuilder
from utils.data_retrieval.session_store import SessionStore


load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env")))


def _sse(event_type: str, content, **kwargs) -> str:
    payload = {"type": event_type, "content": content, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n"


class PortfolioDomainAgent:
    def __init__(self, client: OpenAI | None = None):
        self.domain_key = "portfolio"
        self.display_name = "Portfolio Agent"
        self.client = client or OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self.query_builder = PortfolioQueryBuilder(self.client)
        self.sessions = SessionStore()

    def _event(self, event_type: str, content, **kwargs) -> dict:
        payload = {"type": event_type, "content": content}
        payload.update(kwargs)
        return payload

    def _build_result_payload(self, db_result: dict) -> dict:
        return {
            "domain": self.domain_key,
            "title": "Portfolio Data",
            "columns": db_result.get("columns", []),
            "rows": db_result.get("rows", []),
            "row_count": db_result.get("row_count", 0),
        }

    def execute_events(self, question: str, history: list[dict] | None = None):
        yield self._event("stage", "Portfolio Agent - Building a portfolio SQL query...")
        try:
            result = self.query_builder.run(question, history=history)
        except Exception as exc:
            yield self._event("error", f"Portfolio query generation failed: {exc}", agent=self.domain_key)
            return {
                "status": "error",
                "domain": self.domain_key,
                "error": str(exc),
                "report_chunks": [],
            }

        sql = result.get("sql") or ""
        if sql:
            yield self._event("sql_query", sql, agent=self.domain_key)

        if result.get("status") != "success":
            error = result.get("error") or "Portfolio database query failed."
            yield self._event("error", error, agent=self.domain_key)
            yield self._event("report_chunk", error + "\n", agent=self.domain_key)
            return {
                "status": "error",
                "domain": self.domain_key,
                "sql": sql,
                "error": error,
                "report_chunks": [error],
            }

        db_result = result["db_result"]
        rows = db_result.get("rows", [])
        yield self._event(
            "observation_preview",
            f"Portfolio Agent: retrieved {db_result.get('row_count', 0)} rows.",
            data=rows[:3],
            agent=self.domain_key,
        )
        yield self._event("result_set", self._build_result_payload(db_result), agent=self.domain_key)

        try:
            answer = self.query_builder.summarize(question, sql, rows)
        except Exception:
            answer = json.dumps(rows, indent=2, default=str) if rows else "No matching portfolio records were found."

        yield self._event("report_chunk", answer + "\n", agent=self.domain_key)
        return {
            "status": "success",
            "domain": self.domain_key,
            "sql": sql,
            "db_result": db_result,
            "report_text": answer,
            "report_chunks": [answer],
            "needs_live_web": False,
        }

    def execute_stream(self, question: str, session_id: str | None = None):
        session = self.sessions.get_or_create(session_id)
        self.sessions.add_message(session, "user", question)
        yield _sse("session", {"session_id": session.session_id})
        yield _sse("start", f"Processing: {question}")

        result = None
        generator = self.execute_events(question, history=session.history)
        while True:
            try:
                event = next(generator)
            except StopIteration as stop:
                result = stop.value
                break
            event_type = event.get("type")
            content = event.get("content")
            payload = {k: v for k, v in event.items() if k not in {"type", "content"}}
            yield _sse(event_type, content, **payload)

        report_text = "\n".join((result or {}).get("report_chunks") or []).strip()
        if report_text:
            self.sessions.add_message(session, "assistant", report_text)
        yield _sse("done", "", status=(result or {}).get("status", "unknown"))
