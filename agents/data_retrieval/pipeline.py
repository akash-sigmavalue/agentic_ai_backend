from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from utils.data_retrieval.metrics import AgentMetrics
from agents.data_retrieval_project.pipeline import ProjectDomainAgent
from agents.data_retrieval_portfolio.pipeline import PortfolioDomainAgent
from utils.data_retrieval.session_store import SessionStore
from agents.data_retrieval_transaction.pipeline import TransactionDomainAgent


dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
load_dotenv(dotenv_path)


def _sse(event_type: str, content, **kwargs) -> str:
    payload = {"type": event_type, "content": content, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n"


class UniversalRealEstateAgent:
    def __init__(self):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self.sessions = SessionStore()
        self.transaction_agent = TransactionDomainAgent(self.client)
        self.project_agent = ProjectDomainAgent(self.client)
        self.portfolio_agent = PortfolioDomainAgent(self.client)

    def _normalize_domain(self, selected_domain: str | None) -> str | None:
        domain = (selected_domain or "").strip().lower()
        if domain in {"transaction", "project", "portfolio"}:
            return domain
        return None

    def _resolve_agent(self, selected_domain: str):
        if selected_domain == "transaction":
            return self.transaction_agent
        if selected_domain == "project":
            return self.project_agent
        if selected_domain == "portfolio":
            return self.portfolio_agent
        return None

    def _emit_stage_tokens(self, metrics: AgentMetrics, stage_name: str, usage):
        delta = metrics.add_tokens(usage)
        snap = metrics.snapshot()
        return _sse(
            "token_usage",
            {
                "stage": stage_name,
                "prompt_tokens": delta["prompt_tokens"],
                "completion_tokens": delta["completion_tokens"],
                "total_tokens": delta["total_tokens"],
                "cumulative_total_tokens": snap["total_tokens"],
                "cumulative_cost_usd": snap["cost_usd"],
            },
        )

    def _serialize_domain_event(self, metrics: AgentMetrics, event: dict) -> str | None:
        event_type = event.get("type")
        if event_type == "token_usage_raw":
            return self._emit_stage_tokens(metrics, event.get("stage_name", "unknown"), event.get("usage"))
        if event_type == "report_chunk" and event.get("suppress"):
            return None
        payload = {k: v for k, v in event.items() if k not in {"type", "content", "suppress"}}
        return _sse(event_type, event.get("content"), **payload)

    def _stream_domain_run(self, agent, question: str, metrics: AgentMetrics):
        generator = agent.execute_events(question)
        while True:
            try:
                event = next(generator)
            except StopIteration as stop:
                return stop.value
            serialized = self._serialize_domain_event(metrics, event)
            if serialized:
                yield serialized

    def _append_live_web_context(self, query: str, metrics: AgentMetrics, report_chunks: list[str]):
        yield _sse("error", "External web context is disabled. Only database-backed results are returned.")

    def _accumulate_domain_metrics(self, metrics: AgentMetrics, result: dict | None):
        if not result:
            return
        db_result = result.get("db_result") or {}
        if db_result.get("status") == "success":
            metrics.tools_called += 1
            metrics.sql_retries += db_result.get("retries", 0)
        amenity_result = result.get("amenity_result") or {}
        if amenity_result.get("status") == "success":
            metrics.tools_called += 1

    def _store_clarification(self, session, question: str, clarification_payload: dict):
        self.sessions.set_pending_clarification(
            session=session,
            base_question=question,
            reason=clarification_payload["message"],
            questions=clarification_payload["questions"],
        )
        self.sessions.add_message(session, "assistant", json.dumps(clarification_payload))

    def execute_stream(self, question: str, selected_domain: str | None = None, session_id: str | None = None):
        metrics = AgentMetrics()
        session = self.sessions.get_or_create(session_id)
        self.sessions.add_message(session, "user", question)
        normalized_domain = self._normalize_domain(selected_domain) or self._normalize_domain(question) or "transaction"
        effective_question = question
        if self.sessions.has_pending_clarification(session):
            effective_question = self.sessions.build_effective_question(session, question)

        yield _sse("session", {"session_id": session.session_id})
        yield _sse("start", f"Processing: {question}")
        if self.sessions.has_pending_clarification(session):
            pending = session.pending_clarification or {}
            pending_questions = " ".join(
                question_text
                for turn in (pending.get("turns") or [])
                for question_text in getattr(turn, "questions", [])
            ).lower()
            if "transaction or project" in pending_questions or "either transaction or project" in pending_questions:
                self.sessions.clear_pending_clarification(session)

        agent = self._resolve_agent(normalized_domain)
        yield _sse("stage", f"Main Pipeline: Dispatching to {normalized_domain} agent...")
        yield _sse(
            "agent_route",
            {
                "agent_route": normalized_domain,
                "reason": "Used the domain selected by the user.",
                "confidence": 1.0,
            },
        )

        result = yield from self._stream_domain_run(agent, effective_question, metrics)
        self._accumulate_domain_metrics(metrics, result)

        if result.get("status") == "clarify":
            clarification_payload = result["clarification_payload"]
            self._store_clarification(session, question, clarification_payload)
            yield _sse("clarification_required", clarification_payload)
            yield _sse("done", "", metrics=metrics.finalize())
            return

        self.sessions.clear_pending_clarification(session)

        report_chunks = list(result.get("report_chunks") or [])
        if result.get("needs_live_web"):
            yield from self._append_live_web_context(question, metrics, report_chunks)
        if not report_chunks:
            report_chunks.append("[]")
            yield _sse("report_chunk", "[]\n")
        self.sessions.add_message(session, "assistant", "\n".join(report_chunks).strip())
        yield _sse("done", "", metrics=metrics.finalize())
