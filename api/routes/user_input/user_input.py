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
async def upload_document(file: UploadFile = File(...)) -> dict:
    print("uiehwfuihewufhwe.............")
    require_openai_key()
    (
        create_faiss_retriever,
        create_hybrid_retriever,
        extract_images_from_pdf,
        hybrid_chunking,
        load_documents,
    ) = _load_document_tools()

    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file must have a name.")

    suffix = os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        documents = load_documents(tmp_path, file.filename)
        for document in documents:
            document.metadata["source"] = file.filename

        image_docs = extract_images_from_pdf(tmp_path) if suffix.lower() == ".pdf" else []
        runtime.page_images = {}
        for image_doc in image_docs:
            image_doc.metadata["source"] = file.filename
            page = image_doc.metadata.get("page")
            if isinstance(page, int):
                runtime.page_images.setdefault(page, []).append(image_doc)

        if not documents:
            raise HTTPException(status_code=400, detail="No text could be extracted from the document.")

        chunks = hybrid_chunking(documents)

        if not chunks:
            raise HTTPException(status_code=400, detail="Document did not produce any text chunks.")

        create_faiss_retriever(chunks)
        runtime.ensemble_retriever = create_hybrid_retriever(chunks)

        runtime.document_name = file.filename
        runtime.chunk_count = len(chunks)

        return {
            "document_name": runtime.document_name,
            "pages_or_sections": len(documents),
            "chunk_count": runtime.chunk_count,
            "message": "Document indexed with FAISS + BM25 hybrid retrieval",
            "token_usage": token_usage(),
        }
    finally:
        os.unlink(tmp_path)
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
        retrieval_timing=final_state.get("retrieval_timing"),
    )


@router.post("/ask/stream")
async def ask_stream(request: AskRequest):
    require_openai_key()
    _, rag_graph, _, _, _ = _load_rag_graph()

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    if runtime.faiss_index is None:
        raise HTTPException(status_code=400, detail="Upload and process a document first.")

    async def generate():
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_event_loop()

        with ThreadPoolExecutor() as pool:
            final_state = await loop.run_in_executor(
                pool,
                lambda: rag_graph.invoke(
                    {
                        "question": question,
                        "query_plan": None,
                        "context": [],
                        "answer": "",
                        "retrieval_timing": None,
                        "token_usage": None,
                    }
                ),
            )

        answer = final_state["answer"]
        context_blocks = final_state["context"]
        retrieval_timing = final_state.get("retrieval_timing")
        current_token_usage = final_state.get("token_usage") or {"input": 0, "output": 0}

        yield f"data: {json.dumps({'type': 'token', 'content': answer})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'chunks': context_blocks, 'token_usage': current_token_usage, 'retrieval_timing': retrieval_timing})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
