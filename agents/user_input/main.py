import time
import json
from typing import List, Dict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from agents.user_input.prompts import (
    ANSWER_VERIFICATION_PROMPT,
    QUERY_UNDERSTANDING_PROMPT,
    RAG_PROMPT_TEMPLATE,
)
from core.user_input.config import (
    HYBRID_CANDIDATE_K,
    IMAGE_TOP_PAGES,
    MAX_IMAGES,
    OPENAI_API_KEY,
    RERANK_TOP_K,
)
from api.schemas.user_input import GraphState
from database.user_input_runtime import runtime
from tools.user_input.search import (
    compress_context,
    expand_parent_sections,
    rerank_documents,
)
from utils.user_input.helpers import count_tokens


def _safe_json_loads(text: str) -> dict:
    try:
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned.replace("```json", "", 1).strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.replace("```", "", 1).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
        return json.loads(cleaned)
    except Exception:
        return {}


def _get_llm_token_usage(response, prompt: str, model: str) -> dict[str, int]:
    usage = response.response_metadata.get("token_usage", {})
    if usage:
        return {
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
        }
    return {
        "input": count_tokens(prompt, model),
        "output": count_tokens(response.content, model),
    }


def deduplicate_docs(docs: List) -> List:
    seen = set()
    unique_docs = []
    for doc in docs:
        key = (
            str(doc.metadata.get("source", "")),
            str(doc.metadata.get("page", "")),
            doc.page_content[:300],
        )
        if key not in seen:
            seen.add(key)
            unique_docs.append(doc)
    return unique_docs


def _listify_query_plan_value(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def get_applicability_terms(query_plan: dict) -> List[str]:
    terms = []
    for key in ("mentioned_locations", "mentioned_authorities", "mentioned_regions_or_zones", "mentioned_categories"):
        terms.extend(_listify_query_plan_value(query_plan.get(key)))

    city_specific = query_plan.get("city_specific")
    terms.extend(_listify_query_plan_value(city_specific))

    seen = set()
    unique_terms = []
    for term in terms:
        normalized = term.strip().lower()
        if normalized and normalized not in seen:
            unique_terms.append(term.strip())
            seen.add(normalized)
    return unique_terms


def build_context_blocks(compressed_docs: List) -> List[Dict]:
    """Build context blocks for UI"""
    context_blocks = []
    top_pages = set()

    for index, doc in enumerate(compressed_docs):
        metadata = doc.metadata or {}
        page = metadata.get("page", "unknown")
        source = metadata.get("source", runtime.document_name or "document")

        doc_type = metadata.get("type", "text")
        if metadata.get("is_table"):
            doc_type = "table"

        if page != "unknown" and doc_type != "image" and index < IMAGE_TOP_PAGES:
            top_pages.add(page)

        block = {
            "source": str(source),
            "page": str(page),
            "section": str(metadata.get("section", "")),
            "title": str(metadata.get("title", "")),
            "type": doc_type,
            "chunk_type": str(metadata.get("chunk_type", "")),
            "relevance_score": float(metadata.get("hybrid_score", 0)),
            "rerank_score": float(metadata.get("rerank_score", 0)),
        }

        if doc_type == "image":
            block["image_base64"] = metadata.get("image_base64")
            block["image_mime"] = metadata.get("image_mime", "image/png")
        else:
            block["content"] = doc.page_content

        context_blocks.append(block)

    # Add images (your existing logic)
    image_keys_added = set()
    for page_key in top_pages:
        try:
            page_key = int(page_key)
            for image_doc in runtime.page_images.get(page_key, []):
                meta = image_doc.metadata or {}
                image_key = (page_key, meta.get("image_index", 0))
                if image_key in image_keys_added:
                    continue
                context_blocks.append({
                    "source": str(meta.get("source", runtime.document_name or "document")),
                    "page": str(page_key),
                    "type": "image",
                    "image_base64": meta.get("image_base64"),
                    "image_mime": meta.get("image_mime", "image/png"),
                    "relevance_score": float(meta.get("hybrid_score", 0)),
                    "rerank_score": 0,
                    "section": "",
                    "title": "",
                    "chunk_type": "image",
                })
                image_keys_added.add(image_key)
                if len(image_keys_added) >= MAX_IMAGES:
                    break
        except:
            continue

    return context_blocks


def build_context_string(context_blocks: List[Dict]) -> str:
    context_parts = []
    for context in context_blocks:
        metadata_lines = [
            f"Source: {context['source']}",
            f"Page: {context['page']}",
        ]
        if context.get("section"):
            metadata_lines.append(f"Section: {context['section']}")
        if context.get("title"):
            metadata_lines.append(f"Title: {context['title']}")
        if context.get("type"):
            metadata_lines.append(f"Type: {context['type']}")

        metadata_header = "[" + ", ".join(metadata_lines) + "]"

        if context.get("type") == "image":
            context_parts.append(
                f"{metadata_header}\n"
                f"[IMAGE available for UI rendering]"
            )
        else:
            context_parts.append(
                f"{metadata_header}\n"
                f"{context.get('content', '')}"
            )
    return "\n\n---\n\n".join(context_parts)


# ====================== NODES ======================

def understand_query_node(state: GraphState) -> GraphState:
    question = state["question"]

    prompt = QUERY_UNDERSTANDING_PROMPT.format(question=question)

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
    response = llm.invoke(prompt)

    usage = _get_llm_token_usage(response, prompt, "gpt-4o-mini")

    query_plan = _safe_json_loads(response.content)

    if not query_plan or not isinstance(query_plan, dict):
        query_plan = {
            "main_topic": question,
            "sub_questions": [question],
            "intent_type": "general_document_qa",
            "is_multiple_questions": False,
            "is_mathematical": False,
            "city_specific": None,
            "key_conditions": [],
            "retrieval_queries": [question],
            "missing_information": []
        }

    token_usage = state.get("token_usage") or {"input": 0, "output": 0}
    combined_usage = {
        "input": token_usage.get("input", 0) + usage["input"],
        "output": token_usage.get("output", 0) + usage["output"],
    }

    return {
        **state,
        "query_plan": query_plan,
        "query_understanding_token_usage": usage,
        "token_usage": combined_usage,
    }


def retrieve_node(state: GraphState) -> GraphState:
    if runtime.faiss_index is None:
        return {**state, "answer": "Please upload and process a document first."}

    question = state["question"]
    query_plan = state.get("query_plan", {})
    retrieval_queries = query_plan.get("retrieval_queries", [question])

    all_docs = []
    for rq in retrieval_queries:
        docs = runtime.ensemble_retriever(rq, k=HYBRID_CANDIDATE_K) if hasattr(runtime, 'ensemble_retriever') else []
        all_docs.extend(docs)

    docs = deduplicate_docs(all_docs)
    applicability_terms = get_applicability_terms(query_plan)
    reranked = rerank_documents(
        question,
        docs,
        max_docs=RERANK_TOP_K,
        applicability_terms=applicability_terms,
    )
    expanded = expand_parent_sections(reranked)
    compressed = compress_context(question, expanded)

    context_blocks = build_context_blocks(compressed)

    return {
        **state,
        "context": context_blocks,
        "raw_retrieved_docs": compressed
    }


def generate_node(state: GraphState) -> GraphState:
    if not state.get("context"):
        return {**state, "answer": "No relevant content found in the document."}

    context_str = build_context_string(state["context"])
    prompt = RAG_PROMPT_TEMPLATE.format(
        context_str=context_str,
        query_plan=json.dumps(state.get("query_plan", {}), ensure_ascii=False, indent=2),
        question=state["question"],
    )

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.1, api_key=OPENAI_API_KEY)
    response = llm.invoke(prompt)

    usage = _get_llm_token_usage(response, prompt, "gpt-4o-mini")

    runtime.total_llm_input_tokens += usage["input"]
    runtime.total_llm_output_tokens += usage["output"]

    return {**state, "answer": response.content, "token_usage": usage}


def check_answer_node(state: GraphState) -> GraphState:
    if not state.get("context"):
        return {**state, "answer": "I don't have enough information in the document to answer that."}

    context_str = build_context_string(state["context"])
    draft_answer = state.get("answer", "")

    prompt = ANSWER_VERIFICATION_PROMPT.format(
        question=state["question"],
        query_plan=json.dumps(state.get("query_plan", {}), ensure_ascii=False, indent=2),
        context_str=context_str,
        draft_answer=draft_answer,
    )

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
    response = llm.invoke(prompt)

    checker_usage = _get_llm_token_usage(response, prompt, "gpt-4o-mini")

    token_usage = state.get("token_usage") or {"input": 0, "output": 0}
    combined = {
        "input": token_usage.get("input", 0) + checker_usage["input"],
        "output": token_usage.get("output", 0) + checker_usage["output"],
    }

    return {
        **state,
        "answer": response.content.strip(),
        "verified": True,
        "token_usage": combined,
        "checker_token_usage": checker_usage,
    }


# ====================== GRAPH ======================
builder = StateGraph(GraphState)

builder.add_node("understand_query", understand_query_node)
builder.add_node("retrieve", retrieve_node)
builder.add_node("generate", generate_node)
builder.add_node("check_answer", check_answer_node)

builder.set_entry_point("understand_query")
builder.add_edge("understand_query", "retrieve")
builder.add_edge("retrieve", "generate")
builder.add_edge("generate", "check_answer")
builder.add_edge("check_answer", END)

rag_graph = builder.compile()


def token_usage() -> dict:
    return {
        "input": getattr(runtime, "total_llm_input_tokens", 0),
        "output": getattr(runtime, "total_llm_output_tokens", 0),
    }
