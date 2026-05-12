from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from pydantic import BaseModel


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    chunks: list[dict[str, Any]]
    token_usage: dict[str, int]
    retrieval_timing: dict[str, float] | None = None


class GraphState(TypedDict):
    question: str
    context: list[dict[str, Any]]
    answer: str
    retrieval_timing: NotRequired[dict[str, float] | None]
    token_usage: NotRequired[dict[str, int] | None]
