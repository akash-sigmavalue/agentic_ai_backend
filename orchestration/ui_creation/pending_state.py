import time
import uuid
from typing import Any


PENDING_INTENT_STATES: dict[str, dict[str, Any]] = {}
PENDING_INTENT_TTL_SECONDS = 30 * 60


def cleanup_pending_intent_states() -> None:
    now = time.time()
    expired_plan_ids = [
        plan_id
        for plan_id, state in PENDING_INTENT_STATES.items()
        if now - state.get("created_at", now) > PENDING_INTENT_TTL_SECONDS
    ]
    for plan_id in expired_plan_ids:
        PENDING_INTENT_STATES.pop(plan_id, None)


def store_pending_intent_state(
    user_query: str,
    widget: str | None,
    semantic_schema_dict: dict,
    semantic_schema_json: str,
    component_count: int,
    planner_usage: dict[str, Any] | None,
) -> str:
    cleanup_pending_intent_states()
    plan_id = uuid.uuid4().hex
    PENDING_INTENT_STATES[plan_id] = {
        "created_at": time.time(),
        "user_query": user_query,
        "widget": widget,
        "semantic_schema_dict": semantic_schema_dict,
        "semantic_schema_json": semantic_schema_json,
        "component_count": component_count,
        "planner_usage": planner_usage,
    }
    return plan_id


def pop_pending_intent_state(plan_id: str) -> dict[str, Any]:
    cleanup_pending_intent_states()
    state = PENDING_INTENT_STATES.pop(plan_id, None)
    if state is None:
        raise RuntimeError("Intent plan was not found or has expired. Please run the query again.")
    return state
