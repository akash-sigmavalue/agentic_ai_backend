from fastapi import APIRouter, Depends

from .config import Settings, get_settings
from .llm import OpenAIJsonAgent
from .models import GenerateSqlRequest, PipelineResponse
from .sql_probe import SqlProbeService
from .workflow import SqlAgentWorkflow

router = APIRouter()


def get_workflow(
    request: GenerateSqlRequest,
    settings: Settings = Depends(get_settings),
) -> SqlAgentWorkflow:
    return SqlAgentWorkflow(
        OpenAIJsonAgent(settings, model=request.model),
        sql_probe=SqlProbeService(settings),
        max_react_iterations=settings.react_max_iterations,
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/api/v1/sql/generate", response_model=PipelineResponse)
async def generate_sql(
    request: GenerateSqlRequest,
    workflow: SqlAgentWorkflow = Depends(get_workflow),
) -> PipelineResponse:
    return await workflow.run(
        request.query,
        request.include_intermediate_stages,
        request.semantic_context.model_dump(),
    )
