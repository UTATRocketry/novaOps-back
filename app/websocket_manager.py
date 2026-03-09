from __future__ import annotations

import asyncio
from collections.abc import Iterable

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, str] = {}
        self._connection_order: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, _role_hint: str | None = None) -> str:
        await websocket.accept()

        async with self._lock:
            has_operator = any(role == "operator" for role in self._connections.values())
            assigned_role = "viewer" if has_operator else "operator"
            self._connections[websocket] = assigned_role
            self._connection_order.append(websocket)

        return assigned_role

    async def disconnect(self, websocket: WebSocket) -> None:
        promoted: WebSocket | None = None
        async with self._lock:
            removed_role = self._remove_connection_locked(websocket)
            if removed_role == "operator":
                promoted = self._promote_operator_locked()

        if promoted is not None:
            await self._notify_operator_promotion(promoted)

    async def send_json(self, websocket: WebSocket, payload: dict) -> None:
        await websocket.send_json(payload)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            sockets = list(self._connections.keys())

        stale: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(payload)
            except Exception:
                stale.append(socket)

        promoted: WebSocket | None = None
        if stale:
            async with self._lock:
                removed_operator = False
                for socket in stale:
                    role = self._remove_connection_locked(socket)
                    if role == "operator":
                        removed_operator = True
                if removed_operator:
                    promoted = self._promote_operator_locked()

        if promoted is not None:
            await self._notify_operator_promotion(promoted)

    async def snapshot(self, websocket: WebSocket, actuator_states: dict[str, str]) -> None:
        role = self._connections.get(websocket, "viewer")
        payload = {
            "type": "snapshot",
            "role": role,
            "actuator_states": actuator_states,
        }
        await self.send_json(websocket, payload)

    def roles(self) -> Iterable[str]:
        return self._connections.values()

    def _remove_connection_locked(self, websocket: WebSocket) -> str | None:
        role = self._connections.pop(websocket, None)
        self._connection_order = [ws for ws in self._connection_order if ws is not websocket]
        return role

    def _promote_operator_locked(self) -> WebSocket | None:
        if any(role == "operator" for role in self._connections.values()):
            return None

        for ws in self._connection_order:
            if ws in self._connections:
                self._connections[ws] = "operator"
                return ws
        return None

    async def _notify_operator_promotion(self, websocket: WebSocket) -> None:
        try:
            await self.send_json(
                websocket,
                {
                    "type": "session",
                    "role": "operator",
                    "reassigned": True,
                },
            )
        except Exception:
            await self.disconnect(websocket)
