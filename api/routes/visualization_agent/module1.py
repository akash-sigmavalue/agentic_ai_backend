"""
Visualization Agent Module 1 - FastAPI route.

POST /visualization-agent/module1/run-intent
"""

from datetime import datetime
from threading import Lock
import time

from fastapi import APIRouter, HTTPException

from api.schemas.visualization_agent import Module1Request, Module1Response, TokenLedgerRow
from agents.visualization_agent.services.openai_client import call_openai_for_intent
from agents.visualization_agent.services.demo import demo_intent_output

router = APIRouter(prefix="/visualization-agent", tags=["visualization-agent"])
_request_counter = 0
_request_counter_lock = Lock()


def _next_request_id() -> int:
    global _request_counter
    with _request_counter_lock:
        _request_counter += 1
        return _request_counter


def _build_token_ledger_row(
    *,
    model: str,
    user_query: str,
    usage_data: dict[str, int],
    cost_data: dict[str, float],
) -> TokenLedgerRow:
    return TokenLedgerRow(
        request_id=_next_request_id(),
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        model=model,
        query_preview=user_query[:80],
        input_tokens=usage_data.get("input_tokens", 0),
        cached_input_tokens=usage_data.get("cached_input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        total_cost_usd=cost_data.get("total_cost", 0.0),
    )


@router.post("/module1/run-intent", response_model=Module1Response)
def run_module1_intent(payload: Module1Request) -> Module1Response:
    """
    Accepts a user query, runs Module 1 intent finalization (LLM or demo),
    and returns the repaired structured intent JSON with token/cost data.
    """
    try:
        if payload.demo_mode:
            start_time = time.time()
            intent_output = demo_intent_output(payload.user_query)
            elapsed = round(time.time() - start_time, 2)

            return Module1Response(
                intent_output=intent_output,
                usage={
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_input_tokens": 0,
                },
                cost={
                    "input_cost": 0.0,
                    "cached_input_cost": 0.0,
                    "output_cost": 0.0,
                    "total_cost": 0.0,
                },
                elapsed_seconds=elapsed,
                ledger_row=None,
            )

        intent_output, usage_data, cost_data, elapsed = call_openai_for_intent(
            user_query=payload.user_query,
            model=payload.model,
        )
        ledger_row = _build_token_ledger_row(
            model=payload.model,
            user_query=payload.user_query,
            usage_data=usage_data,
            cost_data=cost_data,
        )

        return Module1Response(
            intent_output=intent_output,
            usage=usage_data,
            cost=cost_data,
            elapsed_seconds=elapsed,
            ledger_row=ledger_row,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
