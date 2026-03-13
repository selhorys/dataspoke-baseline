"""WebSocket authentication handshake helper.

FastAPI WebSocket routes bypass router-level ``dependencies=[]``, so auth
must be handled manually inside the handler via a message-based handshake.
"""

import asyncio
import json

import jwt
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.api.auth.jwt import decode_token


async def ws_authenticate(websocket: WebSocket, timeout: float = 10.0) -> bool:
    """Perform WebSocket auth handshake.

    Expects the first client message to be ``{"type": "auth", "token": "<jwt>"}``.
    Sends ``{"type": "auth_ok"}`` on success or
    ``{"type": "auth_error", "error_code": "UNAUTHORIZED"}`` on failure,
    then closes the connection (code 1008 — Policy Violation).

    Returns ``True`` if authenticated, ``False`` otherwise (connection already
    closed in the failure case).
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
    except (TimeoutError, WebSocketDisconnect):
        await websocket.close(code=1008, reason="auth timeout")
        return False

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.send_json({"type": "auth_error", "error_code": "UNAUTHORIZED"})
        await websocket.close(code=1008)
        return False

    if msg.get("type") != "auth":
        await websocket.send_json({"type": "auth_error", "error_code": "UNAUTHORIZED"})
        await websocket.close(code=1008)
        return False

    try:
        decode_token(msg["token"])
    except (jwt.PyJWTError, KeyError):
        await websocket.send_json({"type": "auth_error", "error_code": "UNAUTHORIZED"})
        await websocket.close(code=1008)
        return False

    await websocket.send_json({"type": "auth_ok"})
    return True
