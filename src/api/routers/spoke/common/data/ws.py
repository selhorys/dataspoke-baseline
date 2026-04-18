"""WebSocket handler: /data/{dataset_urn}/stream/validation"""

import json

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.api.auth.ws import ws_authenticate
from src.api.dependencies import get_redis

sub_router = APIRouter()


@sub_router.websocket("/{dataset_urn}/stream/validation")
async def stream_data_validation(dataset_urn: str, websocket: WebSocket) -> None:
    """Stream validation progress via Redis pub/sub.

    Protocol:
    1. Client sends ``{"type": "auth", "token": "<jwt>"}``
    2. Server replies ``{"type": "auth_ok"}`` then forwards Redis messages
    3. Connection closes after a ``type=summary`` message or client disconnect
    """
    await websocket.accept()

    if not await ws_authenticate(websocket):
        return

    cache = get_redis()
    channel = f"ws:validation:{dataset_urn}"
    try:
        async for message in cache.subscribe(channel):
            await websocket.send_text(message)
            payload = json.loads(message)
            if payload.get("type") == "summary":
                break
    except WebSocketDisconnect:
        pass
    finally:
        await cache.close()
        await websocket.close()
