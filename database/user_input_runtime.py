from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserInputRuntime:
    document_name: str | None = None
    document_names: list[str] = field(default_factory=list)
    chunk_count: int = 0
    chunks: list[Any] = field(default_factory=list)
    embeddings: Any = None
    faiss_index: Any = None
    bm25_retriever: Any = None
    ensemble_retriever: Any = None
    page_images: dict[Any, list[Any]] = field(default_factory=dict)
    loader_type: str | None = None
    total_llm_input_tokens: int = 0
    total_llm_output_tokens: int = 0


runtime = UserInputRuntime()
