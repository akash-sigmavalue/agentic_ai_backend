from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from agents.connector.connector_agent import ConnectorAgent
from agents.connector.workflows.executor import WorkflowExecutor
from agents.connector.workflows.planner import WorkflowPlanner
from api.schemas.connector.request_models import ConnectorTaskRequest, WorkflowRequest
from api.schemas.connector.workflow_models import WorkflowExecutionResult, WorkflowPlan, WorkflowStep


logger = logging.getLogger(__name__)


class SuperAgent:
    """Reasoning layer that plans workflow execution and delegates connector work."""

    def __init__(
        self,
        planner: WorkflowPlanner | None = None,
        executor: WorkflowExecutor | None = None,
        connector_agent: ConnectorAgent | None = None,
    ) -> None:
        self._planner = planner or WorkflowPlanner()
        self._connector_agent = connector_agent or ConnectorAgent()
        self._executor = executor or WorkflowExecutor(self._connector_agent)

    async def handle_request(
        self,
        request: WorkflowRequest,
        db: Session | None = None,
        current_user=None,
    ) -> WorkflowExecutionResult:
        logger.info("super agent handling request")
        plan = await self._planner.create_plan(request, db=db, current_user=current_user)
        return await self._executor.execute(plan, request, db=db, current_user=current_user)

    async def create_demo_plan(self, request: WorkflowRequest) -> WorkflowPlan:
        """Optional helper for tests or docs."""

        return await self._planner.create_plan(request)

    def build_connector_task(self, step: WorkflowStep) -> ConnectorTaskRequest:
        return ConnectorTaskRequest(
            system=step.system or "mock-gmail",
            operation=step.operation or step.name,
            input=step.input,
            requires_approval=step.requires_approval,
        )
