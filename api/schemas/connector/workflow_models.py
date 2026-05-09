from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict


class GmailFilters(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    sender: str | None = Field(default=None, alias="from")
    sender_name: str | None = None
    sender_email: str | None = None
    to: str | None = None
    subject: str | None = None
    keywords: list[str] = Field(default_factory=list)
    is_unread: bool = False
    has_attachment: bool = False
    date_range: str | None = None
    latest: bool = False
    max_results: int = 10


class GmailOutputRequirement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: bool = False
    analysis: bool = False
    analytic_report: bool = False
    reply_required: bool = False
    draft_only: bool = True
    send_directly: bool = False
    tone: str = "professional"


class GmailIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent_type: Literal["gmail"] = "gmail"
    execution_type: Literal["automation_rule", "one_time_action"] = "one_time_action"
    connector: Literal["gmail"] = "gmail"
    trigger_type: str | None = None
    operation: str = "search"
    filters: GmailFilters = Field(default_factory=GmailFilters)
    output_requirement: GmailOutputRequirement = Field(default_factory=GmailOutputRequirement)


class AutomationTrigger(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "gmail.new_email"
    filters: dict[str, Any] = Field(default_factory=dict)


class AutomationAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class AutomationIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent_type: Literal["automation"] = "automation"
    connector: Literal["gmail"] = "gmail"
    trigger: AutomationTrigger = Field(default_factory=AutomationTrigger)
    actions: list[AutomationAction] = Field(default_factory=list)
    extracted_entities: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    """Single execution step in the orchestration plan."""

    id: str
    kind: Literal["analysis", "connector", "approval", "transform", "finalize"]
    name: str
    description: str | None = None
    system: str | None = None
    operation: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    store_as: str | None = None
    foreach: str | None = None
    loop_var: str = "each"


class WorkflowPlan(BaseModel):
    """High-level plan created by the super-agent."""

    type: Literal["workflow", "automation"] = "workflow"
    goal: str
    steps: list[WorkflowStep] = Field(default_factory=list)
    needs_approval: bool = False
    notes: list[str] = Field(default_factory=list)
    gmail_intent: GmailIntent | None = None
    automation_trigger: dict[str, Any] | None = None
    automation_actions: list[dict[str, Any]] = Field(default_factory=list)
    automation_intent: GmailIntent | None = None
    special_response: dict[str, Any] | None = None


class WorkflowExecutionResult(BaseModel):
    """Final response returned to the API layer."""

    success: bool
    summary: str
    status: str | None = None
    final_answer: str | None = None
    message: str | None = None
    rule_id: int | None = None
    missing_field: str | None = None
    question: str | None = None
    partial_intent: dict[str, Any] | None = None
    plan: WorkflowPlan
    step_results: list[dict[str, Any]] = Field(default_factory=list)
    # Temporary compatibility field for legacy frontend contracts.
    raw_mcp_results: list[dict[str, Any]] = Field(default_factory=list)
    approval_status: str = "not_required"
    requires_oauth: bool = False
    connector: str | None = None
    error: str | None = None
    trace_id: str | None = None
    failed_stage: str | None = None
    failed_step: str | None = None
    debug: dict[str, Any] | None = None
