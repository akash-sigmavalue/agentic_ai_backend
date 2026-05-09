from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AutomationPromptRequest(BaseModel):
    prompt: str


class FlowTriggerDraft(BaseModel):
    type: str
    label: str
    conditions: dict[str, Any] = Field(default_factory=dict)


class FlowActionDraft(BaseModel):
    type: str
    label: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class AutomationFlowDraft(BaseModel):
    workflow_name: str
    summary: str
    trigger: FlowTriggerDraft
    actions: list[FlowActionDraft] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    can_execute: bool = False
    reason: str = ""
    requested_outcome: str = ""


class ExecutionPlanStep(BaseModel):
    step: str
    type: Literal["trigger", "action", "guard"]
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class AutomationExecutionPlan(BaseModel):
    can_execute: bool
    steps: list[ExecutionPlanStep] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    unsupported_steps: list[str] = Field(default_factory=list)


class WorkflowNodeSchema(BaseModel):
    id: str
    type: str
    label: str
    status: str = "ready"
    description: str | None = None
    icon: str = "workflow"


class WorkflowConnectionSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")
    label: str | None = None


class AutomationUISchema(BaseModel):
    type: Literal["workflow_builder"] = "workflow_builder"
    title: str
    layout: Literal["horizontal"] = "horizontal"
    scrollable: bool = True
    components: list[WorkflowNodeSchema] = Field(default_factory=list)
    connections: list[WorkflowConnectionSchema] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AutomationFlowResponse(BaseModel):
    flow: AutomationFlowDraft
    execution_plan: AutomationExecutionPlan
    ui_schema: AutomationUISchema
    can_execute: bool
    validation_errors: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
