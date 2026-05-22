from __future__ import annotations

from typing import Any, NotRequired, TypedDict, Optional

from pydantic import BaseModel


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    chunks: list[dict[str, Any]]
    token_usage: dict[str, int]
    verified: bool = False
    retrieval_timing: dict[str, float] | None = None


class GraphState(TypedDict, total=False):
    question: str
    query_plan: Optional[dict[str, Any]]
    context: list[dict[str, Any]]
    answer: str
    retrieval_timing: Optional[dict[str, Any]]
    token_usage: Optional[dict[str, int]]
    query_understanding_token_usage: Optional[dict[str, int]]
    checker_token_usage: Optional[dict[str, int]]
    verified: bool
