"""
Visualization Agent Module 2 - FastAPI route.

POST /visualization-agent/module2/run
"""

import logging
import time

from fastapi import APIRouter, HTTPException

from api.schemas.visualization_agent import Module2Request, Module2Response
from agents.visualization_agent.module2 import run_module_2_from_paths

router = APIRouter(prefix="/visualization-agent", tags=["visualization-agent"])
logger = logging.getLogger(__name__)


@router.post("/module2/run", response_model=Module2Response)
def run_module2(payload: Module2Request) -> Module2Response:
    """
    Run Module 2 data restructuring & filtering pipeline.

    Loads files from disk (with optional path overrides), runs the full
    Module 2 processing pipeline, and returns the structured output.
    """
    try:
        start = time.time()

        inputs_dict = payload.inputs_considered.model_dump()

        output = run_module_2_from_paths(
            inputs_considered=inputs_dict,
            retrieved_data_path=payload.retrieved_data_path,
            data_mapping_path=payload.data_mapping_path,
            module_1_intent_path=payload.module_1_intent_path,
            module_1_intent_json=payload.module_1_intent_json,
            retrieval_context_path=payload.retrieval_context_path,
            retrieval_sql_path=payload.retrieval_sql_path,
        )

        elapsed = round(time.time() - start, 3)
        logger.info("Module 2 completed in %.3fs – status=%s", elapsed, output.get("status"))

        return Module2Response(**output)

    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Module 2 processing failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
