"""Streaming chat endpoints."""

import asyncio
import json
import threading
import time

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from api.routes.web_search.dependencies import get_search_agent
from core.web_search.config import config


router = APIRouter()
STREAM_HEARTBEAT_SECONDS = 15
STREAM_MAX_RUNTIME_SECONDS = 480


@router.get("/chat_stream")
async def chat_stream(
    query: str = Query(..., min_length=1),
    no_cache: bool = False,
    debug: bool = False,
):
    """Stream status messages and answer chunks as server-sent events."""

    async def event_generator():
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        yield f"data: {json.dumps({'type': 'status', 'content': 'Connected to search agent...'})}\n\n"

        def status_callback(message):
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "status", "content": message},
            )

        def stream_callback(message):
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "chunk", "content": message},
            )

        def run_search():
            try:
                agent = get_search_agent()
                result = agent.search(
                    query,
                    max_results=config.MAX_RESULTS_IN_RESPONSE,
                    use_cache=not no_cache,
                    status_callback=status_callback,
                    stream_callback=stream_callback,
                    debug_llm_payloads=debug,
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "done", "result": result},
                )
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "error", "content": str(exc)},
                )

        thread = threading.Thread(target=run_search, daemon=True)
        thread.start()

        started_at = time.monotonic()
        last_keepalive = started_at

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=STREAM_HEARTBEAT_SECONDS,
                    )
                    yield f"data: {json.dumps(item)}\n\n"
                    if item["type"] in {"done", "error"}:
                        break
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - started_at >= STREAM_MAX_RUNTIME_SECONDS:
                        timeout_item = {
                            "type": "error",
                            "content": "Search is taking too long. Please try a narrower query or fewer sources.",
                        }
                        yield f"data: {json.dumps(timeout_item)}\n\n"
                        break

                    if now - last_keepalive >= STREAM_HEARTBEAT_SECONDS:
                        keepalive_item = {
                            "type": "status",
                            "content": "Still working... reading sources and preparing the answer.",
                        }
                        yield f"data: {json.dumps(keepalive_item)}\n\n"
                        last_keepalive = now
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")
