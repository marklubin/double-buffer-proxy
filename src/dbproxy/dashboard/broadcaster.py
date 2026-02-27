"""WebSocket broadcaster for real-time dashboard updates."""

from __future__ import annotations

import json
from typing import Any

import structlog
from aiohttp import web

log = structlog.get_logger()


class Broadcaster:
    """Manages WebSocket connections and broadcasts state changes."""

    def __init__(self) -> None:
        self._connections: set[web.WebSocketResponse] = set()

    def add(self, ws: web.WebSocketResponse) -> None:
        self._connections.add(ws)
        log.debug("ws_client_connected", total=len(self._connections))

    def remove(self, ws: web.WebSocketResponse) -> None:
        self._connections.discard(ws)
        log.debug("ws_client_disconnected", total=len(self._connections))

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Send a JSON message to all connected WebSocket clients."""
        if not self._connections:
            return

        message = json.dumps(data)
        dead: list[web.WebSocketResponse] = []

        for ws in self._connections:
            try:
                await ws.send_str(message)
            except (ConnectionResetError, RuntimeError):
                dead.append(ws)

        for ws in dead:
            self._connections.discard(ws)

    async def broadcast_state(self, manager: Any) -> None:
        """Broadcast a buffer manager's state to all clients."""
        await self.broadcast({
            "type": "state_update",
            "conversation": manager.to_dict(),
        })

    async def broadcast_error(self, conv_id: str, status: int, body: str) -> None:
        """Broadcast an upstream API error to all clients."""
        await self.broadcast({
            "type": "api_error",
            "conv_id": conv_id[:16],
            "status": status,
            "body": body[:1000],
        })

    @property
    def connection_count(self) -> int:
        return len(self._connections)
