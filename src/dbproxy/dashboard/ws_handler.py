"""WebSocket handler for the dashboard."""

from __future__ import annotations

import json

import structlog
from aiohttp import WSMsgType, web

from .broadcaster import Broadcaster

log = structlog.get_logger()


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections from the dashboard."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    broadcaster: Broadcaster = request.app["broadcaster"]
    broadcaster.add(ws)

    try:
        # Send initial state
        registry = request.app["registry"]
        conversations = []
        for mgr in registry.all_conversations().values():
            conversations.append(mgr.to_dict())

        await ws.send_str(json.dumps({
            "type": "initial_state",
            "conversations": conversations,
        }))

        # Listen for client messages (e.g., reset commands)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await _handle_ws_message(request, data)
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning("ws_invalid_message", error=str(exc))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        broadcaster.remove(ws)

    return ws


async def _handle_ws_message(request: web.Request, data: dict) -> None:
    """Handle a message from a WebSocket client."""
    msg_type = data.get("type", "")

    if msg_type == "reset_conversation":
        conv_id = data.get("conv_id", "")
        registry = request.app["registry"]
        for fp, mgr in registry.all_conversations().items():
            if fp.startswith(conv_id):
                await mgr.reset("dashboard")
                log.info("dashboard_reset", conv_id=conv_id)
                break
