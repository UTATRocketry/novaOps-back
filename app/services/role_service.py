from __future__ import annotations

import os
import secrets
from enum import IntEnum

from app.websocket_manager import WebSocketManager


class ClientRole(IntEnum):
    viewer = 0
    pad = 1
    operator = 2
    admin = 3

    @classmethod
    def from_str(cls, value: str) -> ClientRole:
        try:
            return cls[value.lower()]
        except KeyError:
            raise ValueError(f"Unknown role '{value}'. Valid roles: viewer, pad, operator, admin")


class RoleService:
    def __init__(self, ws_manager: WebSocketManager) -> None:
        self._ws_manager = ws_manager
        self._roles: dict[str, ClientRole] = {}
        self._connection_order: list[str] = []

    # ------------------------------------------------------------------
    # Connection lifecycle hooks
    # ------------------------------------------------------------------

    def on_connect(self, client_id: str) -> ClientRole:
        """Register a new connection as viewer. Returns the assigned role."""
        self._roles[client_id] = ClientRole.viewer
        self._connection_order.append(client_id)
        return ClientRole.viewer

    async def on_disconnect(self, client_id: str) -> None:
        """Clean up role state and auto-promote a viewer if the operator left."""
        old_role = self._roles.pop(client_id, None)
        self._connection_order = [cid for cid in self._connection_order if cid != client_id]

        if old_role == ClientRole.operator:
            promoted_id = self._promote_operator()
            if promoted_id is not None:
                await self._notify_role_change(promoted_id, ClientRole.operator, reassigned=True)

    def remove_clients(self, client_ids: list[str]) -> None:
        """Remove role state for a batch of disconnected clients (no auto-promotion)."""
        for cid in client_ids:
            self._roles.pop(cid, None)
        self._connection_order = [cid for cid in self._connection_order if cid not in client_ids]

    # ------------------------------------------------------------------
    # Role assignment
    # ------------------------------------------------------------------

    async def assign_role(self, client_id: str, new_role: ClientRole) -> bool:
        """Assign a role to a connected client. Returns False if client not found.

        Handles operator auto-promotion if the current operator is demoted.
        Exclusive roles (pad, operator, admin): any existing holder is demoted to
        viewer before the new assignment takes effect.
        Notifies the affected client(s) via WebSocket.
        """
        if client_id not in self._roles:
            return False

        # Exclusive roles: demote any existing holder to viewer first.
        if new_role in (ClientRole.pad, ClientRole.operator, ClientRole.admin):
            for cid, role in list(self._roles.items()):
                if cid != client_id and role == new_role:
                    self._roles[cid] = ClientRole.viewer
                    await self._notify_role_change(cid, ClientRole.viewer, reassigned=True)

        old_role = self._roles[client_id]
        self._roles[client_id] = new_role

        if old_role == ClientRole.operator and new_role != ClientRole.operator:
            if not any(r == ClientRole.operator for r in self._roles.values()):
                promoted_id = self._promote_operator()
                if promoted_id is not None:
                    await self._notify_role_change(promoted_id, ClientRole.operator, reassigned=True)

        await self._notify_role_change(client_id, new_role)
        return True

    # ------------------------------------------------------------------
    # Role queries
    # ------------------------------------------------------------------

    def get_role(self, client_id: str | None) -> ClientRole:
        if not client_id:
            return ClientRole.viewer
        return self._roles.get(client_id, ClientRole.viewer)

    def get_role_by_ws(self, websocket) -> ClientRole:
        client_id = self._ws_manager.get_client_id(websocket)
        return self.get_role(client_id)

    def resolve_caller_role(self, x_client_id: str | None) -> ClientRole:
        return self.get_role(x_client_id)

    def list_clients(self) -> list[dict]:
        return [
            {"client_id": cid, "role": self._roles.get(cid, ClientRole.viewer).name}
            for cid in self._ws_manager.list_client_ids()
        ]

    # ------------------------------------------------------------------
    # Permission checks
    # ------------------------------------------------------------------

    def check_command_role(self, role: ClientRole, name: str, state: str | None, config) -> None:
        """Raise ValueError if role is not permitted to send this command.

        Safety rules (see config `safetyRules`):
          - `critical`: per-actuator allowlist of commands a `pad` user may send.
          - `hazardous`: commands that require novaLock to be unlocked (enforced
            separately in apply_command) and that `pad` users may never send.

        operator/admin are not restricted here; hazardous commands are still
        gated by novaLock for every role in apply_command.
        """
        if role < ClientRole.pad:
            raise ValueError("Insufficient role: pad, operator, or admin required to send commands")

        if role == ClientRole.pad:
            if not config.is_critical_command(name, state):
                raise ValueError(
                    "Insufficient role: pad role may only send safety-critical commands listed in config"
                )

    # ------------------------------------------------------------------
    # Admin password
    # ------------------------------------------------------------------

    def get_admin_password(self) -> str | None:
        return os.environ.get("NOVA_ADMIN_PASSWORD") or None

    def verify_admin_password(self, provided: str | None) -> bool:
        expected = self.get_admin_password()
        if expected is None or not provided:
            return False
        return secrets.compare_digest(expected, provided)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _promote_operator(self) -> str | None:
        """Promote the oldest viewer to operator. pad/admin are not auto-promoted."""
        if any(r == ClientRole.operator for r in self._roles.values()):
            return None
        for cid in self._connection_order:
            if self._roles.get(cid) == ClientRole.viewer:
                self._roles[cid] = ClientRole.operator
                return cid
        return None

    async def _notify_role_change(
        self,
        client_id: str,
        role: ClientRole,
        *,
        reassigned: bool = False,
    ) -> None:
        ws = self._ws_manager.get_websocket(client_id)
        if ws is None:
            return
        payload: dict = {"type": "session", "role": role.name, "client_id": client_id}
        if reassigned:
            payload["reassigned"] = True
        try:
            await self._ws_manager.send_json(ws, payload)
        except Exception:
            await self._ws_manager.disconnect(ws)
            self._roles.pop(client_id, None)
            self._connection_order = [cid for cid in self._connection_order if cid != client_id]
