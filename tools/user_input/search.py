import re
from typing import List, Dict, Optional

import faiss
import numpy as np
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from core.user_input.config import (
    HYBRID_CANDIDATE_K,
    MAX_CONTEXT_CHARS,
    OPENAI_API_KEY,
    PARENT_EXPAND_MAX_EXTRA,
    PARENT_EXPAND_TOP_K,
    RERANK_TOP_K,
    RETRIEVAL_BM25_K,
    RETRIEVAL_FAISS_K,
)
from database.user_input_runtime import runtime
from utils.user_input.helpers import is_table_like, is_toc_like, preprocess_for_bm25


# ========================= CONFIG =========================
class ChunkingConfig:
    PRIMARY_CHUNK_SIZE = 1300
    PRIMARY_CHUNK_OVERLAP = 250
    SECTION_PATTERN = r"(?=\n(?:\d{1,3}\.\d+(?:\.\d+){0,5}\b|CHAPTER|SECTION|Regulation|Article|\d+\.\d+\*|\d+\.\d+\#))"
    
    DOMAIN_KEYWORDS = {
        "fsi", "setback", "marginal", "height", "parking", "tdr", "premium",
        "residential", "commercial", "industrial", "congested", "non-congested"
    }


# ========================= CHUNKING =========================
def improved_hierarchical_split(documents: List[Document], config: Optional[ChunkingConfig] = None) -> List[Document]:
    if config is None:
        config = ChunkingConfig()
    
    structured_chunks = []
    
    for doc in documents:
        if doc.metadata.get("type") == "image":
            structured_chunks.append(doc)
            continue

        text = doc.page_content
        page = doc.metadata.get("page")

        raw_sections = re.split(config.SECTION_PATTERN, text)

        current_section = None
        current_parent = None

        for section_text in raw_sections:
            section_text = section_text.strip()
            if len(section_text) < 40:
                continue

            section_match = re.match(r"(\d{1,3}(?:\.\d+){0,5})\b", section_text)
            section_id = section_match.group(1) if section_match else None

            if section_id:
                if "." in section_id:
                    parts = section_id.split(".")
                    current_parent = ".".join(parts[:-1])
                    current_section = section_id
                else:
                    current_parent = None
                    current_section = section_id

            chunk_doc = Document(
                page_content=section_text,
                metadata={
                    **doc.metadata,
                    "section": current_section,
                    "parent_section": current_parent,
                    "original_section": current_section or doc.metadata.get("section"),
                    "title": section_text.split("\n")[0][:150].strip(),
                    "chunk_type": "section",
                    "is_table": is_table_like(section_text),
                    "has_notes": bool(re.search(r"\*|\#|\(\d+\)", section_text[:400])),
                    "page": page,
                }
            )
            structured_chunks.append(chunk_doc)

    return structured_chunks


def hybrid_chunking(documents: List[Document], config: Optional[ChunkingConfig] = None) -> List[Document]:
    if config is None:
        config = ChunkingConfig()

    structured_docs = improved_hierarchical_split(documents, config)
    final_chunks = []

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.PRIMARY_CHUNK_SIZE,
        chunk_overlap=config.PRIMARY_CHUNK_OVERLAP,
        separators=["\n\n\n", "\n\n", "\n", ". ", " ", ""],
        keep_separator=True
    )

    for doc in structured_docs:
        if doc.metadata.get("type") == "image" or len(doc.page_content) <= config.PRIMARY_CHUNK_SIZE + 300:
            final_chunks.append(doc)
            continue

        sub_chunks = text_splitter.split_documents([doc])
        for i, sub in enumerate(sub_chunks):
            sub.metadata = {**doc.metadata, **sub.metadata}
            sub.metadata.update({
                "chunk_type": "sub_chunk",
                "sub_chunk_index": i,
                "parent_chunk_id": doc.metadata.get("section")
            })
        final_chunks.extend(sub_chunks)

    final_chunks = enrich_metadata(final_chunks, config)
    return final_chunks


def enrich_metadata(chunks: List[Document], config: ChunkingConfig) -> List[Document]:
    for chunk in chunks:
        content_lower = chunk.page_content.lower()
        topics = [kw for kw in config.DOMAIN_KEYWORDS if kw in content_lower]
        if topics:
            chunk.metadata["topics"] = topics

        if any(x in content_lower for x in ["congested", "core area", "gaothan"]):
            chunk.metadata["area_type"] = "congested"
        elif any(x in content_lower for x in ["non-congested", "outside congested"]):
            chunk.metadata["area_type"] = "non_congested"

        chunk.metadata["content_length"] = len(chunk.page_content)
    return chunks


# ========================= RETRIEVAL =========================
def create_faiss_retriever(chunks: List[Document]):
    runtime.embeddings = OpenAIEmbeddings(model="text-embedding-ada-002", api_key=OPENAI_API_KEY)
    chunk_texts = [chunk.page_content for chunk in chunks]
    embeddings = runtime.embeddings.embed_documents(chunk_texts)
    
    embeddings_array = np.array(embeddings).astype("float32")
    faiss.normalize_L2(embeddings_array)
    
    index = faiss.IndexFlatIP(len(embeddings_array[0]))
    index.add(embeddings_array)

    runtime.chunks = chunks
    runtime.faiss_index = index


def create_hybrid_retriever(chunks: List[Document], vector_weight: float = 0.65, bm25_weight: float = 0.35):
    bm25_retriever = BM25Retriever.from_documents(chunks, preprocess_func=preprocess_for_bm25)
    bm25_retriever.k = RETRIEVAL_BM25_K

    def hybrid_search(query: str, k: int = HYBRID_CANDIDATE_K) -> List[Document]:
        bm25_docs = bm25_retriever.invoke(query)
        
        query_emb = runtime.embeddings.embed_query(query)
        query_array = np.array([query_emb]).astype("float32")
        faiss.normalize_L2(query_array)
        distances, indices = runtime.faiss_index.search(query_array, RETRIEVAL_FAISS_K)

        faiss_docs = []
        for idx, dist in zip(indices[0], distances[0]):
            if idx != -1 and idx < len(runtime.chunks):
                doc = runtime.chunks[idx]
                doc.metadata["faiss_score"] = float(dist)
                faiss_docs.append(doc)

        # Reciprocal Rank Fusion
        scores: Dict[str, float] = {}
        all_docs = bm25_docs + faiss_docs

        for rank, doc in enumerate(bm25_docs):
            doc_id = id(doc) if hasattr(doc, '__hash__') else doc.page_content[:150]
            scores[doc_id] = scores.get(doc_id, 0) + bm25_weight / (rank + 1)

        for rank, doc in enumerate(faiss_docs):
            doc_id = id(doc) if hasattr(doc, '__hash__') else doc.page_content[:150]
            scores[doc_id] = scores.get(doc_id, 0) + vector_weight / (rank + 1)

        unique_docs = {id(d) if hasattr(d, '__hash__') else d.page_content[:150]: d for d in all_docs}
        for doc in unique_docs.values():
            doc_id = id(doc) if hasattr(doc, '__hash__') else doc.page_content[:150]
            doc.metadata["hybrid_score"] = scores.get(doc_id, 0)

        sorted_docs = sorted(unique_docs.values(), key=lambda x: x.metadata.get("hybrid_score", 0), reverse=True)
        return sorted_docs[:k]

    return hybrid_search


# ========================= POST PROCESSING =========================
def rerank_documents(
    question: str,
    docs: List[Document],
    max_docs: int = RERANK_TOP_K,
    applicability_terms: Optional[List[str]] = None,
) -> List[Document]:
    """Dynamic, LLM-free but intelligent reranker focused on relevance + applicability"""
    q_lower = question.lower()
    q_words = set(re.findall(r"\w+", q_lower))
    normalized_applicability_terms = [
        term.strip().lower()
        for term in (applicability_terms or [])
        if isinstance(term, str) and term.strip()
    ]

    for doc in docs:
        score = float(doc.metadata.get("hybrid_score", 0.0))
        content = doc.page_content.lower()
        metadata = doc.metadata or {}

        section = str(metadata.get("section", "")).lower()
        title = str(metadata.get("title", "")).lower()
        searchable_text = " ".join([content, section, title])

        # === Dynamic Relevance Scoring ===
        overlap = len(q_words & set(re.findall(r"\w+", content)))
        score += overlap * 0.07

        # Boost documents that contain structural elements user might need
        if metadata.get("is_table") or "table" in content:
            score += 0.35

        if re.search(r"\d+\.\d+", section) or "regulation" in content or "chapter" in content:
            score += 0.25

        # === Dynamic Applicability Awareness ===
        # Look for conditions mentioned in query inside the chunk
        condition_keywords = ["road width", "plot area", "sqm", "meter", "width", "area", "zone", "congested"]
        condition_match = sum(1 for kw in condition_keywords if kw in content and kw in q_lower)
        score += condition_match * 0.4

        if normalized_applicability_terms:
            matching_terms = [
                term
                for term in normalized_applicability_terms
                if re.search(rf"\b{re.escape(term)}\b", searchable_text)
            ]
            if matching_terms:
                score += 2.5 + (0.35 * len(matching_terms))
            else:
                score -= 1.25

        # Prefer chunks that mention "basic", "permissible", "standard" when user asks normal case
        if any(word in q_lower for word in ["permissible", "allowed", "basic", "normal", "standard"]):
            if any(word in content for word in ["basic", "permissible without", "without payment", "base fsi"]):
                score += 0.5

        # Slight penalty for very promotional / incentive language unless asked
        incentive_words = ["premium", "incentive", "additional", "tod", "higher", "maximum"]
        if any(word in content for word in incentive_words) and not any(word in q_lower for word in incentive_words):
            score -= 0.25

        doc.metadata["rerank_score"] = max(0.0, score)

    # Sort and return
    return sorted(docs, key=lambda x: x.metadata.get("rerank_score", 0), reverse=True)[:max_docs]


def smart_parent_expansion(docs: List[Document], max_extra: Optional[int] = None) -> List[Document]:
    if max_extra is None:
        max_extra = PARENT_EXPAND_MAX_EXTRA
    
    expanded = list(docs)
    seen = {doc.page_content[:150] for doc in expanded}

    for doc in docs[:PARENT_EXPAND_TOP_K]:
        if len(expanded) >= len(docs) + max_extra:
            break

        section = doc.metadata.get("original_section") or doc.metadata.get("section") or ""
        if not section:
            continue

        for candidate in runtime.chunks:
            if len(expanded) >= len(docs) + max_extra:
                break

            c_sec = candidate.metadata.get("section") or ""
            if (c_sec == section or 
                section.startswith(c_sec + ".") or 
                c_sec.startswith(section + ".")):
                
                key = candidate.page_content[:150]
                if key not in seen:
                    expanded.append(candidate)
                    seen.add(key)
    return expanded


def legal_aware_compressor(question: str, docs: List[Document]) -> List[Document]:
    compressed = []
    total_chars = 0

    for doc in docs:
        if total_chars > MAX_CONTEXT_CHARS:
            break

        if doc.metadata.get("is_table") or doc.metadata.get("has_notes") or len(doc.page_content) < 700:
            compressed.append(doc)
            total_chars += len(doc.page_content)
            continue

        if re.search(r"\d+\.\d+", doc.page_content[:200]):
            compressed.append(doc)
        else:
            compressed.append(Document(
                page_content=doc.page_content,
                metadata=doc.metadata.copy()
            ))
        total_chars += len(compressed[-1].page_content)

    return compressed


# Backward compatibility aliases (important!)
def compress_context(question: str, docs: List[Document]) -> List[Document]:
    return legal_aware_compressor(question, docs)


def expand_parent_sections(reranked_docs: List[Document]) -> List[Document]:
    return smart_parent_expansion(reranked_docs)
