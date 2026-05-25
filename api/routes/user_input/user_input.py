import json
import os
import tempfile
import asyncio

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from agents.user_input.main import token_usage
from api.schemas.user_input import AskRequest, AskResponse
from core.user_input.security import require_openai_key
from database.user_input_runtime import runtime
from langchain_openai import ChatOpenAI
from database.user_input_runtime import runtime

from agents.user_input.main import (
    build_context_string,
    retrieve_node,
    understand_query_node,
)
from agents.user_input.prompts import ANSWER_VERIFICATION_PROMPT
from core.user_input.config import OPENAI_API_KEY
from utils.user_input.helpers import count_tokens
from agents.user_input.prompts import RAG_PROMPT_TEMPLATE
from langchain_core.messages import HumanMessage
import json



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
            create_multi_retriever,
            create_hybrid_retriever_multi,
            extract_images_from_pdf,
            hybrid_chunking,
            load_documents,
        )
    except ModuleNotFoundError as exc:
        raise _missing_dependency_error(exc) from exc

    return create_multi_retriever, create_hybrid_retriever_multi, extract_images_from_pdf, hybrid_chunking, load_documents


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
        create_multi_retriever,
        create_hybrid_retriever_multi,
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

    create_multi_retriever(chunks)
    runtime.ensemble_retriever = create_hybrid_retriever_multi()

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
        

        try:

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
            

            
            context_str = build_context_string(context_blocks)
            prompt = RAG_PROMPT_TEMPLATE.format(
                context_str=context_str,
                query_plan=json.dumps(state.get("query_plan", {}), ensure_ascii=False, indent=2),
                question=state["question"],
            )

            has_images = any(c.get("type") == "image" and c.get("image_base64") for c in context_blocks)
            llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.1, streaming=True, api_key=OPENAI_API_KEY)
            
            if has_images:
                visual_prompt = (
                    f"{prompt}\n\n"
                    "NOTE ON VISUAL CONTEXT: You have been provided with the original base64 images of the document pages "
                    "as visual context alongside the transcribed text context. If you notice any discrepancy in numbers, "
                    "decimal positions, or units of measurement (e.g. 'mm' vs 'm') between the transcribed text context "
                    "and the actual visual content on the image, you MUST trust the exact numbers and units shown on the image."
                )
                content = [{"type": "text", "text": visual_prompt}]
                for c in context_blocks:
                    if c.get("type") == "image" and c.get("image_base64"):
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{c.get('image_mime', 'image/png')};base64,{c['image_base64']}"
                            }
                        })
                messages = [HumanMessage(content=content)]
            else:
                messages = prompt
                
            answer_parts = []
            async for chunk in llm.astream(messages):
                content = getattr(chunk, "content", "") or ""
                if content:
                    answer_parts.append(content)
                    yield _sse({"type": "token", "content": content})
                    
            answer = "".join(answer_parts).strip()
            
            suggested_questions = []
            try:
                suggested_prompt = f"""Based on the following question and answer, generate exactly 4 relevant, specific follow-up questions that a user might want to ask next.
Return them as a JSON list of strings. Do not include markdown formatting or backticks around the JSON.

Question: {state['question']}
Answer: {answer}

JSON format:
[
  "Question 1?",
  "Question 2?",
  "Question 3?",
  "Question 4?"
]
"""
                suggested_response = await llm.ainvoke(suggested_prompt)
                try:
                    cleaned = suggested_response.content.strip()
                    if cleaned.startswith("```json"):
                        cleaned = cleaned.replace("```json", "", 1).strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.replace("```", "", 1).strip()
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3].strip()
                    suggested_questions = json.loads(cleaned)
                except Exception:
                    suggested_questions = []
                if not isinstance(suggested_questions, list):
                    suggested_questions = []
            except Exception:
                pass
            
            usage = {
                "input": count_tokens(prompt, "gpt-4o-mini"),
                "output": count_tokens(answer, "gpt-4o-mini"),
            }
            token_usage = state.get("token_usage") or {"input": 0, "output": 0}
            combined_usage = {
                "input": token_usage.get("input", 0) + usage["input"],
                "output": token_usage.get("output", 0) + usage["output"],
            }
            runtime.total_llm_input_tokens += usage["input"]
            runtime.total_llm_output_tokens += usage["output"]

            yield _sse({
                "type": "done",
                "answer": answer,
                "chunks": context_blocks,
                "token_usage": combined_usage,
                "verified": False,
                "retrieval_timing": state.get("retrieval_timing"),
                "suggested_questions": suggested_questions,
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
