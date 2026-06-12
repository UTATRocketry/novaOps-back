from __future__ import annotations

import asyncio
import uuid

from fastapi import WebSocket


class WebSocketManager:
    """Pure connection registry. No role awareness — role logic lives in RoleService."""

    def __init__(self) -> None:
        self._clients: dict[str, WebSocket] = {}
        self._ws_index: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> str:
        """Accept the socket and register it. Returns the assigned client_id."""
        await websocket.accept()
        client_id = str(uuid.uuid4())
        async with self._lock:
            self._clients[client_id] = websocket
            self._ws_index[websocket] = client_id
        return client_id

    async def disconnect(self, websocket: WebSocket) -> str | None:
        """Remove a connection. Returns the removed client_id, or None if not found."""
        async with self._lock:
            client_id = self._ws_index.pop(websocket, None)
            if client_id is not None:
                self._clients.pop(client_id, None)
        return client_id

    async def send_json(self, websocket: WebSocket, payload: dict) -> None:
        await websocket.send_json(payload)

    async def broadcast(self, payload: dict) -> list[str]:
        """Send payload to all connected clients.

        Returns a list of client_ids that were removed due to send failures
        so callers can clean up associated state (e.g. roles).
        """
        async with self._lock:
            sockets = list(self._clients.items())

        stale_ws: list[WebSocket] = []
        for _, socket in sockets:
            try:
                await socket.send_json(payload)
            except Exception:
                stale_ws.append(socket)

        removed_ids: list[str] = []
        if stale_ws:
            async with self._lock:
                for socket in stale_ws:
                    client_id = self._ws_index.pop(socket, None)
                    if client_id is not None:
                        self._clients.pop(client_id, None)
                        removed_ids.append(client_id)

        return removed_ids

    def get_client_id(self, websocket: WebSocket) -> str | None:
        return self._ws_index.get(websocket)

    def get_websocket(self, client_id: str) -> WebSocket | None:
        return self._clients.get(client_id)

    def list_client_ids(self) -> list[str]:
        return list(self._clients.keys())
