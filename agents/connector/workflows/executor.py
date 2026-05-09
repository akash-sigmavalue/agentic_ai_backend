from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from sqlalchemy.orm import Session

from agents.connector.connector_agent import ConnectorAgent
from agents.connector.policy_agent import PolicyAgent
from agents.connector.services.gmail_workflow_ai import GmailWorkflowAI
from api.schemas.connector.request_models import ConnectorTaskRequest, WorkflowRequest
from api.schemas.connector.workflow_models import WorkflowExecutionResult,GmailIntent, WorkflowPlan, WorkflowStep
from database.connector import crud
from database.connector.automation_crud import create_automation_rule, upsert_contact_mapping


logger = logging.getLogger(__name__)


class WorkflowExecutor:
    """Executes a workflow plan step by step."""

    def __init__(
        self,
        connector_agent: ConnectorAgent,
        gmail_ai: GmailWorkflowAI | None = None,
        policy_agent: PolicyAgent | None = None,
    ) -> None:
        self._connector_agent = connector_agent
        self._gmail_ai = gmail_ai or GmailWorkflowAI()
        self._policy_agent = policy_agent or PolicyAgent()

    async def execute(
        self,
        plan: WorkflowPlan,
        request: WorkflowRequest,
        db: Session | None = None,
        current_user=None,
        trace=None,
    ) -> WorkflowExecutionResult:
        step_results: list[dict[str, Any]] = []
        raw_mcp_results: list[dict[str, Any]] = []
        approval_status = "not_required"
        requires_oauth = False
        oauth_connector: str | None = None
        workflow_error: str | None = None
        failed_stage: str | None = None
        failed_step: str | None = None
        context: dict[str, Any] = {}
        final_result: Any = None

        if plan.type == "automation":
            if db is None or getattr(current_user, "id", None) is None:
                message = "Authentication is required to save automation rules."
                return WorkflowExecutionResult(
                    success=False,
                    summary=message,
                    final_answer=None,
                    message=message,
                    plan=plan,
                    step_results=[],
                    raw_mcp_results=[],
                    approval_status=approval_status,
                    requires_oauth=False,
                    connector="gmail",
                    error=message,
                    failed_stage="workflow_executor",
                    failed_step=None,
                )

            if plan.special_response:
                response = dict(plan.special_response)
                message = str(response.get("question") or "Missing required field")
                return WorkflowExecutionResult(
                    success=False,
                    status=str(response.get("status") or "missing_required_field"),
                    summary=message,
                    final_answer=None,
                    message=message,
                    missing_field=str(response.get("missing_field") or "") or None,
                    question=str(response.get("question") or "") or None,
                    partial_intent=response.get("partial_intent") if isinstance(response.get("partial_intent"), dict) else None,
                    plan=plan,
                    step_results=[],
                    raw_mcp_results=[],
                    approval_status=approval_status,
                    requires_oauth=False,
                    connector="gmail",
                    error=message,
                )

            gmail_intent = plan.gmail_intent
            if gmail_intent is None:
                message = "Automation intent is missing."
                return WorkflowExecutionResult(
                    success=False,
                    status="failed",
                    summary=message,
                    final_answer=None,
                    message=message,
                    plan=plan,
                    step_results=[],
                    raw_mcp_results=[],
                    approval_status=approval_status,
                    requires_oauth=False,
                    connector="gmail",
                    error=message,
                )

            sender_name = gmail_intent.filters.sender_name
            sender_email = gmail_intent.filters.sender_email or gmail_intent.filters.sender
            if sender_name and sender_email and getattr(current_user, "id", None) is not None:
                upsert_contact_mapping(
                    db,
                    user_id=current_user.id,
                    display_name=sender_name,
                    email=sender_email,
                    connector_type="gmail",
                )

            rule = create_automation_rule(
                db,
                user_id=getattr(current_user, "id", None),
                connector_type="gmail",
                trigger_type=gmail_intent.trigger_type or "new_email",
                sender_name=sender_name,
                sender_email=sender_email,
                subject_filter=gmail_intent.filters.subject,
                keyword_filter=list(gmail_intent.filters.keywords or []),
                operation=gmail_intent.operation,
                tone=gmail_intent.output_requirement.tone,
                output_requirement=gmail_intent.output_requirement.model_dump(),
                is_active=True,
                last_processed_message_id=None,
                trigger_filters={
                    "from": sender_email or sender_name,
                    "sender_name": sender_name,
                    "sender_email": sender_email,
                    "subject": gmail_intent.filters.subject,
                    "keywords": list(gmail_intent.filters.keywords or []),
                    "is_unread": False,
                    "has_attachment": bool(gmail_intent.filters.has_attachment),
                },
                actions=[],
            )
            message = self._build_automation_confirmation(gmail_intent, sender_name, sender_email)
            return WorkflowExecutionResult(
                success=True,
                status="automation_rule_created",
                summary=message,
                final_answer=None,
                message=message,
                rule_id=rule.id,
                plan=plan,
                step_results=[],
                raw_mcp_results=[],
                approval_status=approval_status,
                requires_oauth=False,
                connector="gmail",
                error=None,
            )

        for index, step in enumerate(plan.steps, start=1):
            started_at = time.perf_counter()
            resolved_args_snapshot: Any = None
            status = "success"
            error_message: str | None = None
            logger.info("executing step_%s %s", index, step.tool or step.operation or step.name)

            try:
                result, resolved_args_snapshot = await self._execute_step(
                    step,
                    context,
                    request=request,
                    db=db,
                    current_user=current_user,
                    trace=trace,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
                error_message = str(exc)
                status = "fail"
                failed_step = step.id
                failed_stage = "connector_agent" if step.kind == "connector" else "workflow_executor"
                workflow_error = workflow_error or str(exc)
            duration_ms = int((time.perf_counter() - started_at) * 1000)

            context[step.id] = result
            if step.store_as:
                context[step.store_as] = result

            raw_mcp_results.append(self._normalize_result_for_compat(step, result))

            if isinstance(result, dict) and result.get("requires_oauth"):
                requires_oauth = True
                oauth_connector = result.get("connector") or step.system
                workflow_error = result.get("error") or workflow_error
                failed_stage = failed_stage or "connector_agent"
                failed_step = failed_step or step.id
                status = "fail"

            if step.kind == "connector" and isinstance(result, dict) and result.get("ok") is False:
                workflow_error = result.get("error") or workflow_error
                failed_stage = failed_stage or "connector_agent"
                failed_step = failed_step or step.id
                status = "fail"

            if not self._is_success(result):
                status = "fail"
                workflow_error = workflow_error or "Workflow step failed"
                failed_stage = failed_stage or ("connector_agent" if step.kind == "connector" else "workflow_executor")
                failed_step = failed_step or step.id

            logger.info(
                "finished step_%s %s status=%s duration_ms=%s",
                index,
                step.tool or step.operation or step.name,
                status,
                duration_ms,
            )

            step_results.append(
                {
                    "step_id": step.id,
                    "kind": step.kind,
                    "tool": step.tool or step.operation or step.name,
                    "status": "completed" if self._is_success(result) else "failed",
                    "output": self._extract_output(result),
                }
            )

            final_result = result

            if not self._is_success(result):
                break

        summary = self._build_summary(plan, final_result, requires_oauth)
        final_answer = self._build_final_answer(final_result) if not workflow_error and not requires_oauth and self._is_success(final_result) else None

        return WorkflowExecutionResult(
            success=not bool(workflow_error) and not requires_oauth and self._is_success(final_result),
            summary=summary,
            final_answer=final_answer,
            plan=plan,
            step_results=step_results,
            raw_mcp_results=raw_mcp_results,
            approval_status=approval_status,
            requires_oauth=requires_oauth,
            connector=oauth_connector,
            error=workflow_error,
            failed_stage=failed_stage,
            failed_step=failed_step,
        )

    async def _execute_step(
        self,
        step: WorkflowStep,
        context: dict[str, Any],
        *,
        request: WorkflowRequest,
        db: Session | None,
        current_user,
        trace=None,
    ) -> tuple[Any, Any]:
        if step.kind == "approval":
            return {"status": "pending", "message": "Approval step is not yet implemented"}, {"status": "pending"}

        if step.kind == "finalize" and step.output.get("source"):
            resolved = self._resolve_value(step.output["source"], context)
            return resolved, resolved

        if step.foreach:
            items = self._resolve_value(step.foreach, context)
            if not isinstance(items, list):
                items = []

            results: list[Any] = []
            resolved_args_preview: list[Any] = []
            for item in items:
                loop_context = dict(context)
                loop_context[step.loop_var] = item
                resolved_args = self._resolve_value(step.args, loop_context)
                resolved_args_preview.append(resolved_args)
                results.append(
                    await self._dispatch_tool(
                        step,
                        resolved_args,
                        loop_context,
                        request=request,
                        db=db,
                        current_user=current_user,
                        trace=trace,
                    )
                )
            return results, {"foreach": self._resolve_value(step.foreach, context), "items": resolved_args_preview}

        resolved_args = self._resolve_value(step.args, context)
        result = await self._dispatch_tool(
            step,
            resolved_args,
            context,
            request=request,
            db=db,
            current_user=current_user,
            trace=trace,
        )
        return result, resolved_args

    async def _dispatch_tool(
        self,
        step: WorkflowStep,
        args: dict[str, Any] | Any,
        context: dict[str, Any],
        *,
        request: WorkflowRequest,
        db: Session | None,
        current_user,
        trace=None,
    ) -> Any:
        tool_name = step.tool or step.operation or step.name
        if tool_name.startswith("gmail."):
            if tool_name in {"gmail.get_thread", "gmail.reply_to_thread"}:
                thread_id = ""
                if isinstance(args, dict):
                    thread_id = str(args.get("thread_id") or "").strip()
                if not thread_id:
                    logger.warning(
                        "stopping Gmail reply flow because no thread_id was resolved for %s",
                        tool_name,
                    )
                    return self._missing_thread_response(tool_name)
            if tool_name in {"gmail.send_email", "gmail.reply_to_thread", "gmail.draft_email"}:
                return await self._execute_policy_gated_delivery(
                    tool_name,
                    args,
                    request=request,
                    db=db,
                    current_user=current_user,
                    trace=trace,
                )
            task = ConnectorTaskRequest(
                system="gmail",
                operation=tool_name,
                input=args if isinstance(args, dict) else {"value": args},
                requires_approval=step.requires_approval,
            )
            connector_result = await self._connector_agent.execute(
                task,
                db=db,
                user_id=getattr(current_user, "id", None),
                trace=trace,
            )
            return connector_result

        if tool_name.startswith("llm."):
            return self._execute_llm_tool(tool_name, args, context, request=request)

        if step.kind == "analysis":
            return step.output or {"message": "Analysis completed"}

        return step.output or {"message": "Step completed"}

    def _execute_llm_tool(
        self,
        tool_name: str,
        args: dict[str, Any] | Any,
        context: dict[str, Any],
        *,
        request: WorkflowRequest,
    ) -> dict[str, Any]:
        payload = args if isinstance(args, dict) else {"content": args}
        content = payload.get("content") or payload.get("input") or payload.get("context") or payload
        instruction = payload.get("instruction") or request.prompt
        tone = str(payload.get("tone") or "professional")

        if tool_name == "llm.summarize":
            return self._gmail_ai.summarize(content, instruction=str(instruction))
        if tool_name == "llm.analyze":
            return self._gmail_ai.analyze(content, instruction=str(instruction))
        if tool_name == "llm.generate_report":
            return self._gmail_ai.generate_report(content, instruction=str(instruction))
        if tool_name == "llm.generate_reply":
            reply = self._gmail_ai.generate_reply(content, instruction=str(instruction), tone=tone)
            if isinstance(content, dict):
                gmail_data = content.get("data") if isinstance(content.get("data"), dict) else content

                reply["thread_id"] = gmail_data.get("thread_id")
                reply["from_email"] = gmail_data.get("from_email")
                reply["subject"] = gmail_data.get("subject")
            return reply

        return {"message": f"Unsupported LLM tool: {tool_name}"}

    async def _execute_policy_gated_delivery(
        self,
        tool_name: str,
        args: dict[str, Any] | Any,
        *,
        request: WorkflowRequest,
        db: Session | None,
        current_user,
        trace=None,
    ) -> dict[str, Any]:
        payload = args if isinstance(args, dict) else {"value": args}
        thread_id = str(payload.get("thread_id") or "").strip()
        if tool_name == "gmail.reply_to_thread" and not thread_id:
            logger.warning(
                "skipping Gmail reply because no thread_id was provided",
            )
            return self._missing_thread_response(tool_name)
        sender = str(payload.get("to") or payload.get("from_email") or payload.get("sender") or "").strip() or None
        confidence_score = self._estimate_policy_confidence(payload, request)
        rule_config = self._build_policy_rule_config(db, current_user, sender)
        decision = self._policy_agent.decide(
            email_content=payload,
            sender=sender,
            rule_config=rule_config,
            confidence_score=confidence_score,
        )

        if decision.action == "skip":
            return {
                "server": "workflow",
                "connector": "gmail",
                "ok": True,
                "tool": "policy.skip",
                "data": {
                    "status": "skipped",
                    "reason": decision.reason,
                    "sender": sender,
                },
                "raw_mcp_results": {
                    "status": "skipped",
                    "reason": decision.reason,
                    "sender": sender,
                },
            }

        if decision.action == "approval_required":
            return {
                "server": "workflow",
                "connector": "gmail",
                "ok": True,
                "tool": "policy.approval_required",
                "requires_approval": True,
                "data": {
                    "status": "approval_required",
                    "reason": decision.reason,
                    "sender": sender,
                },
                "raw_mcp_results": {
                    "status": "approval_required",
                    "reason": decision.reason,
                    "sender": sender,
                },
            }

        resolved_tool = tool_name
        if decision.action == "draft":
            resolved_tool = "gmail.draft_email"
        elif decision.action == "send" and tool_name not in {"gmail.send_email", "gmail.reply_to_thread"}:
            resolved_tool = "gmail.send_email"

        task = ConnectorTaskRequest(
            system="gmail",
            operation=resolved_tool,
            input=payload,
            requires_approval=False,
        )
        result = await self._connector_agent.execute(
            task,
            db=db,
            user_id=getattr(current_user, "id", None),
            trace=trace,
        )
        if isinstance(result, dict):
            result.setdefault("policy_decision", decision.model_dump())
        return result

    def _missing_thread_response(self, tool_name: str) -> dict[str, Any]:
        message = "No email thread found to reply to. Please check if Gmail has recent messages or adjust the search filter."
        return {
            "server": "workflow",
            "connector": "gmail",
            "ok": False,
            "tool": tool_name,
            "error": message,
            "raw_mcp_results": {
                "ok": False,
                "error": message,
            },
        }

    def _resolve_value(self, value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve_value(item, context) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(item, context) for item in value]
        if isinstance(value, str):
            return self._resolve_string(value, context)
        return value

    def _resolve_string(self, value: str, context: dict[str, Any]) -> Any:
        if value.startswith("$") and value.count("$") == 1 and self._is_simple_reference(value):
            return self._resolve_reference(value[1:], context)

        def replacer(match: re.Match[str]) -> str:
            resolved = self._resolve_reference(match.group(1), context)
            if isinstance(resolved, (dict, list)):
                return self._stringify(resolved)
            if resolved is None:
                return ""
            return str(resolved)

        if "$" in value:
            replaced = re.sub(r"\$([A-Za-z0-9_.]+)", replacer, value)
            return replaced

        return value

    def _is_simple_reference(self, value: str) -> bool:
        return bool(re.fullmatch(r"\$[A-Za-z0-9_.]+", value))

    def _resolve_reference(self, reference: str, context: dict[str, Any]) -> Any:
        parts = reference.split(".")
        current: Any

        if not parts:
            return None

        root = parts[0]
        current = context.get(root)
        if current is None:
            return None

        for part in parts[1:]:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                if part.isdigit():
                    index = int(part)
                    if 0 <= index < len(current):
                        current = current[index]
                    else:
                        return None
                else:
                    return None
            else:
                return None

        return current

    def _normalize_result_for_compat(self, step: WorkflowStep, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        return {
            "server": "google-gmail-api" if step.system == "gmail" else "workflow",
            "connector": step.system,
            "ok": True,
            "tool": step.tool or step.operation or step.name,
            "data": result,
            "raw_mcp_results": result,
        }

    def _extract_output(self, result: Any) -> Any:
        if isinstance(result, dict) and "data" in result:
            return result.get("data")
        return result

    def _is_success(self, result: Any) -> bool:
        if isinstance(result, list):
            return all(self._is_success(item) for item in result)
        if isinstance(result, dict) and "ok" in result:
            return bool(result.get("ok"))
        return True

    def _build_summary(self, plan: WorkflowPlan, final_result: Any, requires_oauth: bool) -> str:
        if requires_oauth:
            return "Workflow paused until Gmail OAuth is connected."
        if plan.gmail_intent and plan.gmail_intent.operation in {"draft_reply", "send_reply"}:
            return "Gmail reply workflow completed."
        if plan.gmail_intent and plan.gmail_intent.operation in {"summarize", "analyze", "report"}:
            return "Gmail analysis workflow completed."
        if plan.gmail_intent and plan.gmail_intent.operation == "search":
            return "Gmail search completed."
        if final_result is None:
            return "Workflow completed."
        if isinstance(final_result, dict):
            if final_result.get("reply"):
                return str(final_result.get("reply"))
            if final_result.get("summary"):
                return str(final_result.get("summary"))
            if final_result.get("report"):
                return str(final_result.get("report"))
            if final_result.get("analysis"):
                return str(final_result.get("analysis"))
        return "Workflow completed."

    def _build_final_answer(self, final_result: Any) -> str | None:
        if final_result is None:
            return None
        if isinstance(final_result, dict):
            for key in ("reply", "summary", "report", "analysis", "message"):
                value = final_result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return self._stringify(final_result)
        return self._stringify(final_result)

    def _extract_reply_body(self, final_result: Any) -> str | None:
        if not isinstance(final_result, dict):
            return None
        for key in ("reply", "message", "summary", "report", "analysis"):
            value = final_result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _derive_gmail_action_status(self, final_result: Any, requires_oauth: bool) -> str:
        if requires_oauth:
            return "oauth_required"
        if isinstance(final_result, list):
            return "completed"
        if isinstance(final_result, dict):
            tool = str(final_result.get("tool") or "")
            if "draft" in tool:
                return "draft_created"
            if "send" in tool or "reply_to_thread" in tool:
                return "sent"
        return "not_required"

    def _build_output_preview(self, step: WorkflowStep, result: Any) -> Any:
        if isinstance(result, dict):
            if result.get("tool") in {"gmail.search_threads", "gmail.get_thread", "gmail.read_message", "gmail.draft_email", "gmail.send_email", "gmail.reply_to_thread"}:
                data = result.get("data") if isinstance(result.get("data"), dict) else result
                if not isinstance(data, dict):
                    return self._stringify_preview(data)
                preview: dict[str, Any] = {
                    "tool": result.get("tool"),
                    "ok": result.get("ok"),
                    "server": result.get("server"),
                }
                for key in ("thread_ids", "first_thread_id", "thread_id", "message_count", "subject", "from_email", "snippet", "id", "status"):
                    if key in data:
                        preview[key] = data.get(key)
                if "threads" in data and isinstance(data.get("threads"), list):
                    preview["threads_count"] = len(data.get("threads") or [])
                if "messages" in data and isinstance(data.get("messages"), list):
                    preview["messages_count"] = len(data.get("messages") or [])
                return preview

            for key in ("reply", "summary", "report", "analysis", "message", "error"):
                value = result.get(key)
                if isinstance(value, str):
                    return {key: self._shorten(value, 300)}
            return self._stringify_preview(result)

        if isinstance(result, list):
            return [self._build_output_preview(step, item) for item in result[:3]]

        return self._stringify_preview(result)

    def _stringify_preview(self, value: Any) -> str:
        try:
            return json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            return str(value)

    def _stringify(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return str(value)
        except Exception:
            return json.dumps(value, default=str)

    def _shorten(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."

    def _estimate_policy_confidence(self, payload: dict[str, Any], request: WorkflowRequest) -> float:
        text = " ".join(
            str(value)
            for value in [
                payload.get("subject"),
                payload.get("body"),
                request.prompt,
            ]
            if value is not None
        ).lower()
        if any(word in text for word in ["urgent", "asap", "please send", "reply now"]):
            return 0.9
        if any(word in text for word in ["thank you", "please", "follow up", "respond"]):
            return 0.65
        return 0.45

    def _build_policy_rule_config(self, db: Session | None, current_user, sender: str | None) -> dict[str, Any]:
        if db is None or getattr(current_user, "id", None) is None or not sender:
            return {"auto_send_allowed": False, "trust_level": 0.0, "require_approval": True}

        preference = crud.get_sender_preference(db, user_id=current_user.id, sender_email=sender)
        if preference is None:
            return {"auto_send_allowed": False, "trust_level": 0.2, "require_approval": True}

        return {
            "auto_send_allowed": bool(preference.auto_send_allowed),
            "trust_level": float(preference.trust_level or 0.0),
            "tone": preference.tone,
            "require_approval": not bool(preference.auto_send_allowed),
        }

    def _build_automation_confirmation(
        self,
        intent: GmailIntent,
        sender_name: str | None,
        sender_email: str | None,
    ) -> str:
        sender_label = sender_name or sender_email or "the sender"
        operation = (intent.operation or "").replace("_", " ")
        tone = intent.output_requirement.tone if intent.output_requirement else "professional"
        if operation == "analyse and reply":
            return f"Automation rule created. I will analyse and reply {tone}ly whenever {sender_label} emails you."
        if intent.output_requirement.draft_only:
            return f"Automation rule created. I will draft a {tone} reply whenever {sender_label} emails you."
        if intent.output_requirement.summary:
            return f"Automation rule created. I will summarize emails from {sender_label} whenever they arrive."
        if intent.output_requirement.analysis and intent.output_requirement.reply_required:
            return f"Automation rule created. I will analyse and reply {tone}ly whenever {sender_label} emails you."
        if intent.output_requirement.reply_required:
            return f"Automation rule created. I will reply {tone}ly whenever {sender_label} emails you."
        return f"Automation rule created successfully for emails from {sender_label}."
