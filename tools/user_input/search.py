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
    SECTION_PATTERN = r"(?=\n(?:\d{1,3}\.\d+(?:\.\d+){0,5}\b|CHAPTER|SECTION|\d+\.\d+\*|\d+\.\d+\#))"
    PAGE_MARKER_PATTERN = r"\[\[PAGE_BREAK:(\d+)\]\]"
    
    DOMAIN_KEYWORDS = {
        "fsi", "setback", "marginal", "height", "parking", "tdr", "premium",
        "residential", "commercial", "industrial", "congested", "non-congested"
    }


# ========================= CHUNKING =========================
def _page_sort_value(page) -> int:
    try:
        return int(page)
    except (TypeError, ValueError):
        return 0


def _format_page_range(pages: List[int]) -> str:
    if not pages:
        return "unknown"

    unique_pages = sorted(set(pages))
    if len(unique_pages) == 1:
        return str(unique_pages[0])

    ranges = []
    start = previous = unique_pages[0]
    for page in unique_pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = page
    ranges.append(f"{start}-{previous}" if start != previous else str(start))
    return ", ".join(ranges)


def merge_pages_for_structural_chunking(documents: List[Document], config: ChunkingConfig) -> List[Document]:
    """Merge page-level PDF docs so structural sections can continue across page breaks."""
    text_docs = [doc for doc in documents if doc.metadata.get("type") != "image"]
    non_text_docs = [doc for doc in documents if doc.metadata.get("type") == "image"]

    from collections import defaultdict
    source_to_docs = defaultdict(list)
    for doc in text_docs:
        source = doc.metadata.get("source", "unknown")
        source_to_docs[source].append(doc)
        
    final_merged = []
    
    for source, docs in source_to_docs.items():
        page_numbers = [_page_sort_value(doc.metadata.get("page")) for doc in docs]
        has_multiple_pages = len({page for page in page_numbers if page}) > 1
        
        if not has_multiple_pages:
            final_merged.extend(docs)
            continue
            
        ordered_docs = sorted(
            enumerate(docs),
            key=lambda item: (_page_sort_value(item[1].metadata.get("page")), item[0]),
        )
        
        merged_parts = []
        merged_pages = []
        base_metadata = docs[0].metadata.copy() if docs else {}
        
        for _, doc in ordered_docs:
            page = _page_sort_value(doc.metadata.get("page"))
            if page:
                merged_pages.append(page)
                merged_parts.append(f"[[PAGE_BREAK:{page}]]\n{doc.page_content.strip()}")
            else:
                merged_parts.append(doc.page_content.strip())
                
        if not merged_parts:
            final_merged.extend(docs)
            continue
            
        merged_text = "\n\n".join(part for part in merged_parts if part)
        base_metadata.update({
            "page": min(merged_pages) if merged_pages else base_metadata.get("page"),
            "pages": sorted(set(merged_pages)),
            "page_range": _format_page_range(merged_pages),
            "chunking_source": "merged_pages",
        })
        final_merged.append(Document(page_content=merged_text, metadata=base_metadata))
        
    return final_merged + non_text_docs


def improved_hierarchical_split(documents: List[Document], config: Optional[ChunkingConfig] = None) -> List[Document]:
    if config is None:
        config = ChunkingConfig()
    
    structured_chunks = []
    
    main_heading = ""
    
    for doc in documents:
        if doc.metadata.get("type") == "image":
            structured_chunks.append(doc)
            continue

        text = doc.page_content
        page = doc.metadata.get("page")
        active_page = _page_sort_value(page)
        pending_page = None

        raw_sections = re.split(config.SECTION_PATTERN, text)

        current_section = None
        current_parent = None

        for section_text in raw_sections:
            section_text = section_text.strip()

            if pending_page:
                active_page = pending_page
                pending_page = None

            trailing_markers = re.findall(
                rf"(?:\s*{config.PAGE_MARKER_PATTERN})+\s*$",
                section_text,
            )
            if trailing_markers:
                pending_page = int(trailing_markers[-1])
                section_text = re.sub(
                    rf"(?:\s*{config.PAGE_MARKER_PATTERN})+\s*$",
                    "",
                    section_text,
                ).strip()

            marker_pages = [
                int(page)
                for page in re.findall(config.PAGE_MARKER_PATTERN, section_text)
            ]
            section_pages = ([active_page] if active_page else []) + marker_pages
            section_text = re.sub(config.PAGE_MARKER_PATTERN, "", section_text).strip()
            if marker_pages:
                active_page = marker_pages[-1]

            if len(section_text) < 40:
                continue

            section_match = re.match(r"(\d{1,3}(?:\.\d+){0,5})\b", section_text)
            section_id = section_match.group(1) if section_match else None

            if section_id:
                if "." in section_id:
                    parts = section_id.split(".")
                    # If it's a top level like 14.2, update main_heading
                    if len(parts) == 2:
                        main_heading = section_text.split("\n")[0][:200].strip()
                    
                    current_parent = ".".join(parts[:-1])
                    current_section = section_id
                else:
                    current_parent = None
                    current_section = section_id
                    main_heading = section_text.split("\n")[0][:200].strip()

            # Prepend main heading if this is a sub-section
            final_content = section_text
            if main_heading and section_id and section_id.count(".") >= 2:
                if main_heading not in section_text:
                    final_content = f"{main_heading}\n\n{section_text}"

            chunk_doc = Document(
                page_content=final_content,
                metadata={
                    **doc.metadata,
                    "section": current_section,
                    "parent_section": current_parent,
                    "original_section": current_section or doc.metadata.get("section"),
                    "title": section_text.split("\n")[0][:150].strip(),
                    "chunk_type": "section",
                    "is_table": is_table_like(section_text),
                    "has_notes": bool(re.search(r"\*|\#|\(\d+\)", section_text[:400])),
                    "page": min(section_pages) if section_pages else page,
                    "pages": sorted(set(section_pages)) if section_pages else doc.metadata.get("pages"),
                    "page_range": _format_page_range(section_pages) if section_pages else doc.metadata.get("page_range"),
                }
            )
            structured_chunks.append(chunk_doc)

    return structured_chunks


def hybrid_chunking(documents: List[Document], config: Optional[ChunkingConfig] = None) -> List[Document]:
    if config is None:
        config = ChunkingConfig()

    documents = merge_pages_for_structural_chunking(documents, config)

    # Get structural sections only
    final_chunks = improved_hierarchical_split(documents, config)

    # RECURSIVE CHUNKING IS NOW COMMENTED OUT AS REQUESTED
    # text_splitter = RecursiveCharacterTextSplitter(
    #     chunk_size=config.PRIMARY_CHUNK_SIZE,
    #     chunk_overlap=config.PRIMARY_CHUNK_OVERLAP,
    #     separators=["\n\n\n", "\n\n", "\n", ". ", " ", ""],
    #     keep_separator=True
    # )

    # for doc in structured_docs:
    #     if doc.metadata.get("type") == "image" or len(doc.page_content) <= config.PRIMARY_CHUNK_SIZE + 300:
    #         final_chunks.append(doc)
    #         continue

    #     sub_chunks = text_splitter.split_documents([doc])
    #     for i, sub in enumerate(sub_chunks):
    #         sub.metadata = {**doc.metadata, **sub.metadata}
    #         sub.metadata.update({
    #             "chunk_type": "sub_chunk",
    #             "sub_chunk_index": i,
    #             "parent_chunk_id": doc.metadata.get("section")
    #         })
    #     final_chunks.extend(sub_chunks)

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
def create_multi_retriever(chunks: List[Document]):
    if not runtime.embeddings:
        runtime.embeddings = OpenAIEmbeddings(model="text-embedding-3-small", api_key=OPENAI_API_KEY)

    text_chunks, table_chunks, image_chunks = [], [], []
    for chunk in chunks:
        c_type = chunk.metadata.get("content_type")
        if not c_type:
            c_type = "table" if chunk.metadata.get("is_table") else "text"
        
        if c_type == "table":
            table_chunks.append(chunk)
        elif c_type == "image":
            image_chunks.append(chunk)
        else:
            text_chunks.append(chunk)
            
    runtime.text_chunks = text_chunks
    runtime.table_chunks = table_chunks
    runtime.image_chunks = image_chunks

    def _build_faiss(target_chunks):
        if not target_chunks:
            return None
        chunk_texts = [c.page_content for c in target_chunks]
        embeddings = runtime.embeddings.embed_documents(chunk_texts)
        embeddings_array = np.array(embeddings).astype("float32")
        faiss.normalize_L2(embeddings_array)
        index = faiss.IndexFlatIP(len(embeddings_array[0]))
        index.add(embeddings_array)
        return index

    runtime.text_faiss_index = _build_faiss(text_chunks)
    runtime.table_faiss_index = _build_faiss(table_chunks)
    runtime.image_faiss_index = _build_faiss(image_chunks)

    runtime.faiss_index = _build_faiss(chunks)
    runtime.chunks = chunks


def create_hybrid_retriever_multi(vector_weight: float = 0.6, bm25_weight: float = 0.4):
    def _build_bm25(target_chunks):
        if not target_chunks:
            return None
        retriever = BM25Retriever.from_documents(target_chunks, preprocess_func=preprocess_for_bm25)
        retriever.k = RETRIEVAL_BM25_K
        return retriever

    text_bm25 = _build_bm25(runtime.text_chunks)
    table_bm25 = _build_bm25(runtime.table_chunks)
    image_bm25 = _build_bm25(runtime.image_chunks)
    fallback_bm25 = _build_bm25(runtime.chunks)

    def _hybrid_search_target(query: str, target_type: str = "mixed", k: int = HYBRID_CANDIDATE_K) -> List[Document]:
        if target_type == "table" and runtime.table_faiss_index and table_bm25:
            idx, bm25, chunks = runtime.table_faiss_index, table_bm25, runtime.table_chunks
        elif (target_type == "image" or target_type == "figure_diagram") and runtime.image_faiss_index and image_bm25:
            idx, bm25, chunks = runtime.image_faiss_index, image_bm25, runtime.image_chunks
        elif target_type == "text" and runtime.text_faiss_index and text_bm25:
            idx, bm25, chunks = runtime.text_faiss_index, text_bm25, runtime.text_chunks
        else:
            idx, bm25, chunks = runtime.faiss_index, fallback_bm25, runtime.chunks
            
        if not idx or not bm25:
            return []

        bm25_docs = bm25.invoke(query)
        
        query_emb = runtime.embeddings.embed_query(query)
        query_array = np.array([query_emb]).astype("float32")
        faiss.normalize_L2(query_array)
        distances, indices = idx.search(query_array, RETRIEVAL_FAISS_K)

        faiss_docs = []
        for index_val, dist in zip(indices[0], distances[0]):
            if index_val != -1 and index_val < len(chunks):
                doc = chunks[index_val]
                doc.metadata["faiss_score"] = float(dist)
                faiss_docs.append(doc)

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

    return _hybrid_search_target


# ========================= POST PROCESSING =========================
def rerank_documents(
    question: str,
    docs: List[Document],
    max_docs: int = RERANK_TOP_K,
    applicability_terms: Optional[List[str]] = None,
    query_plan: Optional[Dict] = None,
) -> List[Document]:
    """Dynamic, LLM-free but intelligent reranker focused on relevance + applicability"""
    q_lower = question.lower()
    q_words = set(re.findall(r"\w+", q_lower))
    query_plan = query_plan or {}
    normalized_applicability_terms = [
        term.strip().lower()
        for term in (applicability_terms or [])
        if isinstance(term, str) and term.strip()
    ]
    aggregation_value = query_plan.get("aggregation_required")
    aggregation_required = aggregation_value is True or str(aggregation_value).strip().lower() in {"true", "yes", "1"}
    plan_terms = []
    for key in ("calculation_targets", "table_columns_to_retrieve"):
        value = query_plan.get(key)
        if isinstance(value, str):
            plan_terms.append(value)
        elif isinstance(value, list):
            plan_terms.extend(item for item in value if isinstance(item, str))

    retrieval_queries = query_plan.get("retrieval_queries") or []
    if isinstance(retrieval_queries, str):
        retrieval_queries = [retrieval_queries]
    plan_text = " ".join([
        *plan_terms,
        *(item for item in retrieval_queries if isinstance(item, str)),
    ]).lower()
    needs_fsi_components = (
        aggregation_required
        or "fsi" in q_lower
        or "fsi" in plan_text
        or "development potential" in q_lower
        or "building potential" in q_lower
        or "building potential" in plan_text
    )
    component_terms = [
        term.lower()
        for term in plan_terms
        if isinstance(term, str) and term.strip()
    ]
    if needs_fsi_components:
        component_terms.extend([
            "basic fsi",
            "base fsi",
            "premium fsi",
            "fsi on payment",
            "payment of premium",
            "tdr",
            "tdr loading",
            "maximum permissible tdr",
            "maximum building potential",
            "building potential",
            "ancillary fsi",
            "additional fsi",
        ])

    seen_component_terms = set()
    component_terms = [
        term for term in component_terms
        if term and not (term in seen_component_terms or seen_component_terms.add(term))
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

        if aggregation_required and (metadata.get("is_table") or "table" in content):
            score += 0.6

        if component_terms:
            matched_components = [
                term for term in component_terms
                if re.search(rf"\b{re.escape(term)}\b", searchable_text)
            ]
            if matched_components:
                score += min(2.0, 0.3 * len(matched_components))

        if needs_fsi_components and any(
            term in content
            for term in ["maximum building potential", "building potential on plot", "maximum permissible tdr", "fsi on payment"]
        ):
            score += 0.75

        # === Dynamic Applicability Awareness ===
        # Look for conditions mentioned in query inside the chunk
        condition_keywords = ["road width", "plot area", "sqm", "sq.m", "sq.m.", "meter", "m.", "width", "area", "zone", "congested"]
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
        if (
            not needs_fsi_components
            and any(word in content for word in incentive_words)
            and not any(word in q_lower for word in incentive_words)
        ):
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
        # if total_chars > MAX_CONTEXT_CHARS:
        #     break

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
