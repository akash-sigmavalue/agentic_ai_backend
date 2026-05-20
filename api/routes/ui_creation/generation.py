from __future__ import annotations

import asyncio
import json
import os
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

# Authentication/authorization is currently disabled.
# from auth.dependencies import get_current_user
from orchestration.ui_creation.pipeline import (
    execute_analysis_pipeline,
    execute_analysis_pipeline_stream,
    resume_analysis_pipeline_stream,
)
from agents.uploaded_data_grounding_agent.main import (
    ingest_uploaded_file,
    validate_uploaded_table_for_user,
)


router = APIRouter(prefix="/generation", tags=["generation"])
AUTH_DISABLED_USER_ID = 1


@router.post("/query")
def generate_analysis(
    payload: dict,
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
):
    try:
        user_query = payload.get("query", "").strip()
        widget = payload.get("widget")

        if not user_query:
            raise HTTPException(status_code=400, detail="Query is required")

        file_id = payload.get("file_id") or payload.get("fileId") or payload.get("uploaded_table_name")
        if file_id:
            # Authorization check disabled.
            # validate_uploaded_table_for_user(current_user.id, file_id)
            pass

        result = execute_analysis_pipeline(
            user_query=user_query,
            widget=widget,
            uploaded_table_name=file_id,
        )

        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _sse_format(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.post("/upload-data")
async def upload_analysis_file(
    file: UploadFile = File(...),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
):
    suffix = os.path.splitext(file.filename or "")[1]
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = temp_file.name
            temp_file.write(await file.read())

        table_name = ingest_uploaded_file(
            user_id=AUTH_DISABLED_USER_ID,
            file_path=temp_path,
            filename=file.filename or "upload",
        )

        return {"file_id": table_name}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@router.get("/stream")
async def generate_analysis_stream(
    query: str = Query(..., description="User query"),
    widget: str | None = Query(None, description="Selected widget"),
    file_id: str | None = Query(None, description="Uploaded file table id"),
    fileId: str | None = Query(None, description="Uploaded file table id"),
    uploaded_table_name: str | None = Query(None, description="Uploaded file table id"),
    pause_after_intent: bool = Query(False, description="Pause after Agent 1 and wait for file decision"),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
):
    async def event_generator():
        try:
            selected_file_id = file_id or fileId or uploaded_table_name
            if selected_file_id:
                # Authorization check disabled.
                # validate_uploaded_table_for_user(current_user.id, selected_file_id)
                pass

            for event in execute_analysis_pipeline_stream(
                user_query=query,
                widget=widget,
                uploaded_table_name=selected_file_id,
                pause_after_intent=pause_after_intent,
            ):
                yield _sse_format(event)
                await asyncio.sleep(0.05)

        except Exception as e:
            yield _sse_format(
                {
                    "event_type": "error",
                    "node": "system",
                    "message": str(e),
                    "data": {},
                }
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stream/resume")
async def resume_analysis_stream(
    plan_id: str = Form(...),
    file_id: str | None = Form(None),
    fileId: str | None = Form(None),
    uploaded_table_name: str | None = Form(None),
    file: UploadFile | None = File(None),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
):
    async def event_generator():
        temp_path = None
        try:
            selected_file_id = file_id or fileId or uploaded_table_name

            if file is not None:
                suffix = os.path.splitext(file.filename or "")[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                    temp_path = temp_file.name
                    temp_file.write(await file.read())

                selected_file_id = ingest_uploaded_file(
                    user_id=AUTH_DISABLED_USER_ID,
                    file_path=temp_path,
                    filename=file.filename or "upload",
                )
            elif selected_file_id:
                # Authorization check disabled.
                # validate_uploaded_table_for_user(current_user.id, selected_file_id)
                pass

            for event in resume_analysis_pipeline_stream(
                plan_id=plan_id,
                uploaded_table_name=selected_file_id,
            ):
                yield _sse_format(event)
                await asyncio.sleep(0.05)

        except Exception as e:
            yield _sse_format(
                {
                    "event_type": "error",
                    "node": "system",
                    "message": str(e),
                    "data": {},
                }
            )
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
