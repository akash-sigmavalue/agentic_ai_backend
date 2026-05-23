"""
Visualization Agent Module 3.1 - Dynamic map builder route.

POST /visualization-agent/module31/generate
"""

from fastapi import APIRouter, HTTPException

from api.schemas.visualization_agent import Module31Request, Module31Response
from agents.visualization_agent.services.module31 import generate_module31_map


router = APIRouter(prefix="/visualization-agent", tags=["visualization-agent"])


@router.post("/module31/generate", response_model=Module31Response)
def generate_module31(payload: Module31Request) -> Module31Response:
    try:
        result = generate_module31_map(
            module1_output=payload.module_1_intent_json,
            module2_output=payload.module_2_output_json,
            model=payload.model,
        )
        return Module31Response(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
