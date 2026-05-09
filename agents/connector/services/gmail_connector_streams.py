from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, AsyncGenerator


class GmailConnectorEventHub:
    def __init__(self) -> None:
        self._listeners: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, connector_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._listeners[connector_id].add(queue)
        return queue

    def unsubscribe(self, connector_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        listeners = self._listeners.get(connector_id)
        if not listeners:
            return
        listeners.discard(queue)
        if not listeners:
            self._listeners.pop(connector_id, None)

    async def publish(self, connector_id: str, event: dict[str, Any]) -> None:
        listeners = list(self._listeners.get(connector_id, set()))
        if not listeners:
            return

        for queue in listeners:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue


gmail_connector_event_hub = GmailConnectorEventHub()


def build_sse_payload(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


async def stream_gmail_connector_events(connector_id: str) -> AsyncGenerator[str, None]:
    queue = gmail_connector_event_hub.subscribe(connector_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield build_sse_payload(event)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        gmail_connector_event_hub.unsubscribe(connector_id, queue)
