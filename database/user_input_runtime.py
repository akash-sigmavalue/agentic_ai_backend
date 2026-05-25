from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserInputRuntime:
    document_name: str | None = None
    document_names: list[str] = field(default_factory=list)
    chunk_count: int = 0
    chunks: list[Any] = field(default_factory=list)
    
    text_chunks: list[Any] = field(default_factory=list)
    table_chunks: list[Any] = field(default_factory=list)
    image_chunks: list[Any] = field(default_factory=list)
    
    embeddings: Any = None
    
    faiss_index: Any = None
    text_faiss_index: Any = None
    table_faiss_index: Any = None
    image_faiss_index: Any = None
    
    bm25_retriever: Any = None
    
    ensemble_retriever: Any = None
    text_ensemble_retriever: Any = None
    table_ensemble_retriever: Any = None
    image_ensemble_retriever: Any = None
    
    page_images: dict[Any, list[Any]] = field(default_factory=dict)
    loader_type: str | None = None
    total_llm_input_tokens: int = 0
    total_llm_output_tokens: int = 0


runtime = UserInputRuntime()
