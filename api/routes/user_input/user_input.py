import json
import os
import tempfile

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from agents.user_input.main import token_usage
from api.schemas.user_input import AskRequest, AskResponse
from core.user_input.security import require_openai_key
from database.user_input_runtime import runtime


router = APIRouter()
print("reach route .................")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _missing_dependency_error(exc: ModuleNotFoundError) -> HTTPException:
    package_name = exc.name or "unknown"
    hint = " Install the RAG dependencies, for example: pip install faiss-cpu" if package_name == "faiss" else ""
    return HTTPException(status_code=500, detail=f"Missing RAG dependency: {package_name}.{hint}")


def _load_document_tools():
    try:
        from agents.user_input.tools import (
            create_faiss_retriever,
            create_hybrid_retriever,
            extract_images_from_pdf,
            hybrid_chunking,
            load_documents,
        )
    except ModuleNotFoundError as exc:
        raise _missing_dependency_error(exc) from exc

    return create_faiss_retriever, create_hybrid_retriever, extract_images_from_pdf, hybrid_chunking, load_documents


def _load_rag_graph():
    try:
        from agents.user_input.main import build_context_string, rag_graph, retrieve_node, token_usage
        from agents.user_input.prompts import RAG_PROMPT_TEMPLATE
    except ModuleNotFoundError as exc:
        raise _missing_dependency_error(exc) from exc

    return build_context_string, rag_graph, retrieve_node, token_usage, RAG_PROMPT_TEMPLATE


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/")
def root() -> dict:
    return {
        "name": "RAG Document Reader API with FAISS",
        "status": "running",
        "endpoints": {
            "health": "/health",
            "upload_document": "POST /documents",
            "ask_question": "POST /ask",
            "status": "/status",
        },
    }


@router.get("/status")
def status() -> dict:
    return {
        "document_name": runtime.document_name,
        "chunk_count": runtime.chunk_count,
        "token_usage": {
            "input": runtime.total_llm_input_tokens,
            "output": runtime.total_llm_output_tokens,
        },
        "retrieval_method": "hybrid (FAISS + BM25)",
        "has_faiss": runtime.faiss_index is not None,
        "has_bm25": runtime.bm25_retriever is not None,
        "has_images": len(runtime.page_images) > 0,
        "reranker_type": "lightweight_rule_based",
        "compression_type": "rule_based",
        "loader_type": runtime.loader_type or "unknown",
    }

print("upper............")
@router.post("/documents")
async def upload_documents(files: list[UploadFile] = File(...)) -> dict:
    print("uiehwfuihewufhwe.............")
    require_openai_key()
    (
        create_faiss_retriever,
        create_hybrid_retriever,
        extract_images_from_pdf,
        hybrid_chunking,
        load_documents,
    ) = _load_document_tools()

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    all_documents = []
    runtime.page_images = {}
    document_names = []

    for file in files:
        if not file.filename:
            continue

        document_names.append(file.filename)
        suffix = os.path.splitext(file.filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        try:
            documents = load_documents(tmp_path, file.filename)
            for document in documents:
                document.metadata["source"] = file.filename
            all_documents.extend(documents)

            if suffix.lower() == ".pdf":
                image_docs = extract_images_from_pdf(tmp_path)
                for image_doc in image_docs:
                    image_doc.metadata["source"] = file.filename
                    page = image_doc.metadata.get("page")
                    if isinstance(page, int):
                        runtime.page_images.setdefault((file.filename, page), []).append(image_doc)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    if not all_documents:
        raise HTTPException(status_code=400, detail="No text could be extracted from any of the documents.")

    chunks = hybrid_chunking(all_documents)

    if not chunks:
        raise HTTPException(status_code=400, detail="Documents did not produce any text chunks.")

    create_faiss_retriever(chunks)
    runtime.ensemble_retriever = create_hybrid_retriever(chunks)

    runtime.document_name = ", ".join(document_names)
    runtime.document_names = document_names
    runtime.chunk_count = len(chunks)

    return {
        "document_name": runtime.document_name,
        "pages_or_sections": len(all_documents),
        "chunk_count": runtime.chunk_count,
        "message": "Documents indexed with FAISS + BM25 hybrid retrieval",
        "token_usage": token_usage(),
    }
print("lower............")

@router.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    require_openai_key()
    _, rag_graph, _, _, _ = _load_rag_graph()

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    if runtime.faiss_index is None:
        raise HTTPException(status_code=400, detail="Upload and process a document first.")

    final_state = rag_graph.invoke(
        {
            "question": question,
            "query_plan": None,
            "context": [],
            "answer": "",
            "retrieval_timing": None,
            "token_usage": None,
        }
    )
    return AskResponse(
        answer=final_state["answer"],
        chunks=final_state["context"],
        token_usage=final_state.get("token_usage") or {"input": 0, "output": 0},
        verified=bool(final_state.get("verified")),
        retrieval_timing=final_state.get("retrieval_timing"),
        suggested_questions=final_state.get("suggested_questions") or [],
    )


@router.post("/ask/stream")
async def ask_stream(request: AskRequest):
    require_openai_key()

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    if runtime.faiss_index is None:
        raise HTTPException(status_code=400, detail="Upload and process a document first.")

    async def generate():
        import asyncio

        try:
            from langchain_openai import ChatOpenAI

            from agents.user_input.main import (
                build_context_string,
                generate_node,
                retrieve_node,
                understand_query_node,
            )
            from agents.user_input.prompts import ANSWER_VERIFICATION_PROMPT
            from core.user_input.config import OPENAI_API_KEY
            from utils.user_input.helpers import count_tokens

            loop = asyncio.get_running_loop()
            state = {
                "question": question,
                "query_plan": None,
                "context": [],
                "answer": "",
                "retrieval_timing": None,
                "token_usage": None,
            }

            yield _sse({"type": "status", "stage": "understand_query", "content": "Understanding query"})
            state = await loop.run_in_executor(None, lambda: understand_query_node(state))

            yield _sse({"type": "status", "stage": "retrieve", "content": "Retrieving document context"})
            state = await loop.run_in_executor(None, lambda: retrieve_node(state))

            context_blocks = state.get("context") or []
            if not context_blocks:
                answer = state.get("answer") or "No relevant content found in the document."
                yield _sse({"type": "token", "content": answer})
                yield _sse({
                    "type": "done",
                    "answer": answer,
                    "chunks": [],
                    "token_usage": state.get("token_usage") or {"input": 0, "output": 0},
                    "verified": False,
                    "retrieval_timing": state.get("retrieval_timing"),
                    "suggested_questions": [],
                })
                return

            yield _sse({"type": "status", "stage": "generate", "content": "Drafting answer"})
            state = await loop.run_in_executor(None, lambda: generate_node(state))

            # yield _sse({"type": "status", "stage": "check_answer", "content": "Verifying answer"})
            # context_str = build_context_string(context_blocks)
            # draft_answer = state.get("answer", "")
            # prompt = ANSWER_VERIFICATION_PROMPT.format(
            #     question=question,
            #     query_plan=json.dumps(state.get("query_plan", {}), ensure_ascii=False, indent=2),
            #     context_str=context_str,
            #     draft_answer=draft_answer,
            # )

            # llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, streaming=True, api_key=OPENAI_API_KEY)
            # answer_parts = []
            # async for chunk in llm.astream(prompt):
            #     content = getattr(chunk, "content", "") or ""
            #     if not content:
            #         continue
            #     answer_parts.append(content)
            #     yield _sse({"type": "token", "content": content})

            # answer = "".join(answer_parts).strip() or draft_answer
            # checker_usage = {
            #     "input": count_tokens(prompt, "gpt-4o-mini"),
            #     "output": count_tokens(answer, "gpt-4o-mini"),
            # }
            # token_usage = state.get("token_usage") or {"input": 0, "output": 0}
            # current_token_usage = {
            #     "input": token_usage.get("input", 0) + checker_usage["input"],
            #     "output": token_usage.get("output", 0) + checker_usage["output"],
            # }

            yield _sse({
                "type": "done",
                "answer": state.get("answer", ""),
                "chunks": context_blocks,
                "token_usage": state.get("token_usage") or {"input": 0, "output": 0},
                "verified": False,
                "retrieval_timing": state.get("retrieval_timing"),
                "suggested_questions": state.get("suggested_questions") or [],
            })
        except Exception as exc:
            yield _sse({"type": "error", "content": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
