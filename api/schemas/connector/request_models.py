from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkflowRequest(BaseModel):
    """User-facing request payload for the orchestration API."""

    prompt: str = Field(..., min_length=1, description="Natural language task from the user")
    team_context: dict[str, Any] = Field(default_factory=dict, description="Optional team or workspace context")
    debug: bool = Field(default=False, description="Include a verbose workflow trace in the response")


class ConnectorTaskRequest(BaseModel):
    """Structured task passed from the workflow executor to the connector agent."""

    system: str = Field(..., description="Target connector system, for example gmail")
    operation: str = Field(..., description="Generic operation name such as list_attachments")
    input: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False


class ApprovalDecision(BaseModel):
    """Represents a human approval gate decision."""

    required: bool = False
    status: Literal["approved", "pending", "not_required"] = "not_required"
    reason: str | None = None
