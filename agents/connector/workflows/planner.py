from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from agents.connector.services.gmail_workflow_ai import GmailWorkflowAI
from api.schemas.connector.request_models import WorkflowRequest
from api.schemas.connector.workflow_models import GmailIntent, WorkflowPlan, WorkflowStep
from database.connector import crud
from utils.connector.gmail_client import GmailAPIClient, GmailAPIError
# from app.schemas.request_models import WorkflowRequest
# from app.schemas.workflow_models import GmailIntent, WorkflowPlan, WorkflowStep
# from app.google_api.gmail_client import GmailAPIClient, GmailAPIError
# from app.services.gmail_workflow_ai import GmailWorkflowAI


logger = logging.getLogger(__name__)


class WorkflowPlanner:
    """Turns a user prompt into a dynamic workflow plan."""

    def __init__(self, gmail_ai: GmailWorkflowAI | None = None) -> None:
        self._gmail_ai = gmail_ai or GmailWorkflowAI()

    async def create_plan(
        self,
        request: WorkflowRequest,
        db: Session | None = None,
        current_user=None,
    ) -> WorkflowPlan:
        prompt = request.prompt.strip()
        intent = self._gmail_ai.extract_intent(prompt)

        logger.info("planner received prompt")

        if intent is not None and getattr(intent, "intent_type", None) == "gmail":
            if intent.execution_type == "automation_rule":
                plan = await self._create_automation_plan(prompt, intent, db=db, current_user=current_user)
                logger.info("planner detected automation intent")
                return plan

            plan = self._create_gmail_plan(prompt, intent)
            logger.info("planner detected gmail intent")
            return plan

        plan = self._create_generic_plan(prompt)
        logger.info("planner generated generic plan")
        return plan

    def _create_gmail_plan(self, prompt: str, intent: GmailIntent) -> WorkflowPlan:
        steps: list[WorkflowStep] = []
        query = self._gmail_ai.build_gmail_query(intent.filters)
        workflow_debug: list[dict[str, object]] = []

        logger.info(
            "normalized gmail filters=%s query=%s",
            intent.filters.model_dump(by_alias=True),
            query,
        )

        super_agent_debug = {
            "detected_intent": f"gmail.{intent.operation}",
            "detected_connector": "gmail",
            "confidence": self._estimate_confidence(intent),
            "reasoning_summary": self._build_reasoning_summary(prompt, intent, query),
            "extracted_entities": {
                "sender": intent.filters.sender,
                "recipient": intent.filters.to,
                "subject": intent.filters.subject,
                "keywords": intent.filters.keywords,
                "date_filter": intent.filters.date_range,
                "latest": intent.filters.latest,
                "unread": intent.filters.is_unread,
            },
            "generated_gmail_query": query,
            "output_requirements": intent.output_requirement.model_dump(),
        }

        step_1 = WorkflowStep(
            id="step_1",
            kind="analysis",
            name="interpret_gmail_intent",
            description="Extract Gmail intent and filter criteria from the user prompt.",
            output=intent.model_dump(by_alias=True),
        )
        steps.append(step_1)
        workflow_debug.append(
            {
                "step": 1,
                "step_id": step_1.id,
                "tool": "intent_extraction",
                "input_args": {"prompt": prompt},
                "why_selected": "The prompt is Gmail-related, so the planner first extracts intent and filters before choosing tools.",
            }
        )

        search_step = WorkflowStep(
            id="step_2",
            kind="connector",
            name="search_gmail_threads",
            system="gmail",
            operation="gmail.search_threads",
            tool="gmail.search_threads",
            args={
                "query": query,
                "max_results": intent.filters.max_results,
            },
            description="Search Gmail using the extracted filters.",
            output={"expected": "thread ids and thread list"},
            store_as="search",
        )
        steps.append(search_step)
        workflow_debug.append(
                {
                    "step": 2,
                    "step_id": search_step.id,
                    "tool": search_step.tool,
                    "input_args": search_step.args,
                "why_selected": self._search_step_reason(intent, query),
            }
        )

        if intent.operation == "search":
            final_step = WorkflowStep(
                id="step_3",
                kind="finalize",
                name="return_search_results",
                description="Return the matching Gmail threads.",
                output={"source": "step_2"},
            )
            steps.append(final_step)
            workflow_debug.append(
                {
                    "step": 3,
                    "step_id": final_step.id,
                    "tool": "finalize",
                    "input_args": final_step.output,
                    "why_selected": "The user asked for search results only, so the workflow can finish after retrieving matching threads.",
                }
            )

        if intent.operation in {"draft_reply", "send_reply"}:
            workflow_debug.append(
                {
                    "step": "validation",
                    "step_id": "reply_thread_guard",
                    "tool": "validation",
                    "input_args": {
                        "requires_thread_id": True,
                        "latest": intent.filters.latest,
                    },
                    "why_selected": "Reply workflows must stop early when no Gmail thread is found.",
                }
            )

        needs_thread_details = intent.operation in {"read", "summarize", "analyze", "report", "draft_reply", "send_reply"}
        if needs_thread_details:
            if intent.filters.latest or intent.filters.max_results == 1:
                step_3 = WorkflowStep(
                    id="step_3",
                    kind="connector",
                    name="fetch_latest_thread",
                    system="gmail",
                    operation="gmail.get_thread",
                    tool="gmail.get_thread",
                    args={"thread_id": "$step_2.first_thread_id"},
                    description="Fetch the latest matching Gmail thread.",
                    output={"expected": "thread with messages"},
                    store_as="thread",
                )
                steps.append(step_3)
                workflow_debug.append(
                    {
                        "step": 3,
                        "step_id": step_3.id,
                        "tool": step_3.tool,
                        "input_args": step_3.args,
                        "why_selected": "The prompt asks for the latest or a single thread, so the workflow fetches the most recent match before analysis or reply generation.",
                    }
                )
            else:
                step_3 = WorkflowStep(
                    id="step_3",
                    kind="connector",
                    name="fetch_matching_threads",
                    system="gmail",
                    operation="gmail.get_thread",
                    tool="gmail.get_thread",
                    foreach="$step_2.thread_ids",
                    loop_var="thread_id",
                    args={"thread_id": "$each"},
                    description="Fetch each matching Gmail thread.",
                    output={"expected": "list of threads"},
                    store_as="threads",
                )
                steps.append(step_3)
                workflow_debug.append(
                    {
                        "step": 3,
                        "step_id": step_3.id,
                        "tool": step_3.tool,
                        "input_args": step_3.args,
                        "why_selected": "The prompt can match multiple threads, so the workflow fetches every matching Gmail thread before running the LLM step.",
                    }
                )

        if intent.operation in {"summarize", "analyze", "report"}:
            tool_name = {
                "summarize": "llm.summarize",
                "analyze": "llm.analyze",
                "report": "llm.generate_report",
            }[intent.operation]
            label = {
                "summarize": "summarize_gmail_thread",
                "analyze": "analyze_gmail_threads",
                "report": "generate_gmail_report",
            }[intent.operation]
            step_4 = WorkflowStep(
                id="step_4",
                kind="analysis",
                name=label,
                tool=tool_name,
                args={
                    "content": "$step_3",
                    "instruction": prompt,
                    "tone": intent.output_requirement.tone,
                },
                description="Use an LLM to transform Gmail content into the requested output.",
                output={"expected": intent.operation},
                store_as="llm_output",
            )
            steps.append(step_4)
            workflow_debug.append(
                {
                    "step": 4,
                    "step_id": step_4.id,
                    "tool": step_4.tool,
                    "input_args": step_4.args,
                    "why_selected": f"The user requested a {intent.operation} output, so the workflow passes the Gmail content through the matching LLM tool.",
                }
            )

        if intent.operation in {"draft_reply", "send_reply"}:
            if intent.filters.latest or intent.filters.max_results == 1:
                step_4 = WorkflowStep(
                    id="step_4",
                    kind="analysis",
                    name="generate_gmail_reply",
                    tool="llm.generate_reply",
                    args={
                        "content": "$step_3",
                        "instruction": prompt,
                        "tone": intent.output_requirement.tone,
                    },
                    description="Generate a reply from the fetched Gmail thread content.",
                    output={"expected": "reply text"},
                    store_as="reply",
                )
                steps.append(step_4)
                workflow_debug.append(
                    {
                        "step": 4,
                        "step_id": step_4.id,
                        "tool": step_4.tool,
                        "input_args": step_4.args,
                        "why_selected": "The user wants a reply, so the workflow first generates reply text from the fetched Gmail thread.",
                    }
                )
            else:
                step_4 = WorkflowStep(
                    id="step_4",
                    kind="analysis",
                    name="generate_gmail_replies",
                    tool="llm.generate_reply",
                    foreach="$step_3",
                    loop_var="each",
                    args={
                        "content": "$each",
                        "instruction": prompt,
                        "tone": intent.output_requirement.tone,
                    },
                    description="Generate replies for each fetched Gmail thread.",
                    output={"expected": "reply list"},
                    store_as="replies",
                )
                steps.append(step_4)
                workflow_debug.append(
                    {
                        "step": 4,
                        "step_id": step_4.id,
                        "tool": step_4.tool,
                        "input_args": step_4.args,
                        "why_selected": "The user wants replies for multiple matching threads, so the workflow generates one reply per thread.",
                    }
                )

            if intent.output_requirement.send_directly:
                step_5 = WorkflowStep(
                    id="step_5",
                    kind="connector",
                    name="send_gmail_reply",
                    system="gmail",
                    operation="gmail.reply_to_thread",
                    tool="gmail.reply_to_thread",
                    foreach="$step_4" if not (intent.filters.latest or intent.filters.max_results == 1) else None,
                    loop_var="each",
                    args={
                        "thread_id": "$step_3.data.thread_id" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.thread_id",
                        "to": "$step_3.data.from_email" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.from_email",
                        "subject": "$step_3.data.subject" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.subject",
                        "body": "$step_4.reply" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.reply",
                    },
                    description="Send the generated reply in the Gmail thread.",
                    output={"expected": "sent reply"},
                )
                steps.append(step_5)
                workflow_debug.append(
                    {
                        "step": 5,
                        "step_id": step_5.id,
                        "tool": step_5.tool,
                        "input_args": step_5.args,
                        "why_selected": "The user explicitly requested sending, so the generated reply is sent back through the original Gmail thread.",
                    }
                )
            else:
                step_5 = WorkflowStep(
                    id="step_5",
                    kind="connector",
                    name="create_gmail_draft",
                    system="gmail",
                    operation="gmail.draft_email",
                    tool="gmail.draft_email",
                    foreach="$step_4" if not (intent.filters.latest or intent.filters.max_results == 1) else None,
                    loop_var="each",
                    args={
                        "thread_id": "$step_3.data.thread_id" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.thread_id",
                        "to": "$step_3.data.from_email" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.from_email",
                        "subject": "$step_3.data.subject" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.subject",
                        "body": "$step_4.reply" if (intent.filters.latest or intent.filters.max_results == 1) else "$each.reply",
                    },
                    description="Create a draft reply in Gmail.",
                    output={"expected": "draft created"},
                )
                steps.append(step_5)
                workflow_debug.append(
                    {
                        "step": 5,
                        "step_id": step_5.id,
                        "tool": step_5.tool,
                        "input_args": step_5.args,
                        "why_selected": "The prompt calls for a reply but does not clearly request sending, so the workflow creates a draft for safety.",
                    }
                )

        notes = [
            "Dynamic Gmail workflow generated from the prompt.",
            "Reply workflows draft by default unless the prompt clearly requests sending.",
        ]
        return WorkflowPlan(goal=prompt, steps=steps, notes=notes, gmail_intent=intent)

    async def _create_automation_plan(
        self,
        prompt: str,
        intent: GmailIntent,
        *,
        db: Session | None,
        current_user=None,
    ) -> WorkflowPlan:
        resolved_intent, missing_response = await self._resolve_automation_sender(prompt, intent, db=db, current_user=current_user)

        if missing_response is not None:
            return WorkflowPlan(
                type="automation",
                goal=prompt,
                steps=[],
                notes=["Automation intent detected, but the sender email is required before the rule can be created."],
                gmail_intent=resolved_intent,
                special_response=missing_response,
            )

        return WorkflowPlan(
            type="automation",
            goal=prompt,
            steps=[],
            notes=["Automation rule prepared for Gmail new-email execution."],
            gmail_intent=resolved_intent,
            automation_intent=resolved_intent,
        )

    async def _resolve_automation_sender(
        self,
        prompt: str,
        intent: GmailIntent,
        *,
        db: Session | None,
        current_user=None,
    ) -> tuple[GmailIntent, dict[str, object] | None]:
        sender_name = (intent.filters.sender_name or "").strip() or None
        sender_candidate = (intent.filters.sender_email or intent.filters.sender or "").strip() or None
        sender_email = sender_candidate if sender_candidate and self._looks_like_email(sender_candidate) else None

        if sender_email:
            intent.filters.sender_email = sender_email.lower()
            intent.filters.sender = sender_email.lower()
            return intent, None

        if sender_candidate and self._looks_like_email(sender_candidate):
            intent.filters.sender_email = sender_candidate.lower()
            intent.filters.sender = sender_candidate.lower()
            if sender_name is None:
                sender_name = sender_candidate
            intent.filters.sender_name = sender_name
            return intent, None

        if sender_name and db is not None and getattr(current_user, "id", None) is not None:
            mapping = crud.find_contact_mapping_by_name(
                db,
                user_id=current_user.id,
                display_name=sender_name,
                connector_type="gmail",
            )
            if mapping is not None:
                intent.filters.sender_email = mapping.email
                intent.filters.sender = mapping.email
                intent.filters.sender_name = sender_name
                return intent, None

            gmail_resolved = await self._resolve_from_gmail_history(db, current_user, sender_name)
            if gmail_resolved:
                intent.filters.sender_email = gmail_resolved
                intent.filters.sender = gmail_resolved
                intent.filters.sender_name = sender_name
                return intent, None

        if sender_name:
            partial_intent = intent.model_dump()
            partial_intent["filters"]["from"] = sender_name
            partial_intent["filters"]["sender_name"] = sender_name
            partial_intent["filters"]["sender_email"] = None
            question = f"What is {sender_name}'s email address?"
            return intent, {
                "status": "missing_required_field",
                "missing_field": "sender_email",
                "question": question,
                "partial_intent": partial_intent,
            }

        return intent, None

    async def _resolve_from_gmail_history(self, db: Session, current_user, sender_name: str) -> str | None:
        connection = crud.get_oauth_connection(
            db,
            user_id=current_user.id,
            provider="google",
            system="gmail",
        )
        if connection is None or not connection.access_token:
            return None

        client = GmailAPIClient()
        try:
            search_result = await client.search_threads(
                connection.access_token,
                query=f'from:"{sender_name}"',
                max_results=5,
            )
        except GmailAPIError:
            return None

        threads = search_result.get("threads")
        if not isinstance(threads, list):
            return None

        for thread in threads:
            if not isinstance(thread, dict):
                continue
            thread_id = str(thread.get("id") or "").strip()
            if not thread_id:
                continue
            try:
                thread_data = await client.get_thread(connection.access_token, thread_id)
            except GmailAPIError:
                continue
            sender_email = self._extract_sender_email_from_thread(thread_data, sender_name)
            if sender_email:
                return sender_email
        return None

    def _extract_sender_email_from_thread(self, thread: dict[str, Any], sender_name: str) -> str | None:
        messages = thread.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        latest_message = messages[-1] if isinstance(messages[-1], dict) else {}
        headers = latest_message.get("payload", {}).get("headers") if isinstance(latest_message.get("payload"), dict) else []
        if not isinstance(headers, list):
            return None
        for header in headers:
            if not isinstance(header, dict):
                continue
            if str(header.get("name") or "").lower() != "from":
                continue
            value = str(header.get("value") or "").strip()
            if not value:
                continue
            if sender_name.lower() in value.lower():
                extracted = self._extract_email_from_from_header(value)
                if extracted:
                    return extracted
        return None

    def _extract_email_from_from_header(self, header_value: str) -> str | None:
        match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", header_value)
        if match:
            return match.group(0).strip().lower()
        return None

    def _looks_like_email(self, value: str) -> bool:
        return bool(re.fullmatch(r"[\w\.-]+@[\w\.-]+\.\w+", value.strip()))

    def _create_generic_plan(self, prompt: str) -> WorkflowPlan:
        steps = [
            WorkflowStep(
                id="step_1",
                kind="analysis",
                name="interpret_user_intent",
                description="General-purpose analysis step for a non-specialized request.",
                output={"intent": "generic_workflow"},
            ),
            WorkflowStep(
                id="step_2",
                kind="finalize",
                name="return_result",
                description="Return a generic structured response.",
            ),
        ]
        notes = [
            "This is a starter planner. Replace heuristics with an LLM-driven planner later.",
        ]
        return WorkflowPlan(goal=prompt, steps=steps, notes=notes)

    def _estimate_confidence(self, intent: GmailIntent) -> float:
        confidence = 0.8
        if intent.filters.sender:
            confidence += 0.08
        if intent.filters.subject:
            confidence += 0.04
        if intent.filters.keywords:
            confidence += 0.04
        if intent.filters.latest:
            confidence += 0.02
        return min(confidence, 0.99)

    def _build_reasoning_summary(self, prompt: str, intent: GmailIntent, query: str) -> str:
        parts = [f"Detected Gmail workflow for {intent.operation}."]
        if query:
            parts.append(f"Generated Gmail query: {query}.")
        if intent.filters.sender:
            parts.append(f"Sender filter extracted from prompt: {intent.filters.sender}.")
        if intent.filters.subject:
            parts.append(f"Subject filter extracted from prompt: {intent.filters.subject}.")
        if intent.filters.keywords:
            parts.append(f"Keyword filters extracted: {', '.join(intent.filters.keywords)}.")
        return " ".join(parts)

    def _search_step_reason(self, intent: GmailIntent, query: str) -> str:
        if query:
            return "The user asked to locate Gmail messages first, so the workflow starts with Gmail search using the extracted filters."
        return "The workflow starts with Gmail search because the request is Gmail-related and needs thread discovery first."
