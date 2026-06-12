import asyncio

from app.services.role_service import ClientRole, RoleService
from app.websocket_manager import WebSocketManager


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.messages: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def _make_pair() -> tuple[WebSocketManager, RoleService]:
    manager = WebSocketManager()
    service = RoleService(manager)
    return manager, service


def test_all_new_clients_default_to_viewer() -> None:
    async def run() -> None:
        manager, service = _make_pair()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()

        cid1 = await manager.connect(ws1)
        role1 = service.on_connect(cid1)
        cid2 = await manager.connect(ws2)
        role2 = service.on_connect(cid2)

        assert role1 == ClientRole.viewer
        assert role2 == ClientRole.viewer

    asyncio.run(run())


def test_assign_role_promotes_client() -> None:
    async def run() -> None:
        manager, service = _make_pair()
        ws = FakeWebSocket()
        cid = await manager.connect(ws)
        service.on_connect(cid)

        ok = await service.assign_role(cid, ClientRole.operator)
        assert ok
        assert service.get_role(cid) == ClientRole.operator

        session_messages = [m for m in ws.messages if m.get("type") == "session"]
        assert any(m.get("role") == "operator" for m in session_messages)

    asyncio.run(run())


def test_viewer_promoted_to_operator_when_operator_disconnects() -> None:
    async def run() -> None:
        manager, service = _make_pair()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()

        cid1 = await manager.connect(ws1)
        service.on_connect(cid1)
        cid2 = await manager.connect(ws2)
        service.on_connect(cid2)

        await service.assign_role(cid1, ClientRole.operator)
        await manager.disconnect(ws1)
        await service.on_disconnect(cid1)

        assert any(
            m.get("type") == "session" and m.get("role") == "operator"
            for m in ws2.messages
        )

    asyncio.run(run())


def test_pad_role_not_auto_promoted() -> None:
    async def run() -> None:
        manager, service = _make_pair()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()

        cid1 = await manager.connect(ws1)
        service.on_connect(cid1)
        cid2 = await manager.connect(ws2)
        service.on_connect(cid2)

        await service.assign_role(cid1, ClientRole.operator)
        await service.assign_role(cid2, ClientRole.pad)

        await manager.disconnect(ws1)
        await service.on_disconnect(cid1)

        assert service.get_role(cid2) == ClientRole.pad

    asyncio.run(run())


def test_list_clients() -> None:
    async def run() -> None:
        manager, service = _make_pair()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()

        cid1 = await manager.connect(ws1)
        service.on_connect(cid1)
        cid2 = await manager.connect(ws2)
        service.on_connect(cid2)

        await service.assign_role(cid1, ClientRole.operator)

        clients = service.list_clients()
        assert len(clients) == 2
        roles = {c["client_id"]: c["role"] for c in clients}
        assert roles[cid1] == "operator"
        assert roles[cid2] == "viewer"

    asyncio.run(run())


def test_assign_role_returns_false_for_unknown_client() -> None:
    async def run() -> None:
        _, service = _make_pair()
        ok = await service.assign_role("nonexistent-id", ClientRole.operator)
        assert not ok

    asyncio.run(run())


def test_remove_clients_cleans_up_stale_entries() -> None:
    async def run() -> None:
        manager, service = _make_pair()
        ws = FakeWebSocket()
        cid = await manager.connect(ws)
        service.on_connect(cid)

        service.remove_clients([cid])
        assert service.get_role(cid) == ClientRole.viewer  # default fallback

    asyncio.run(run())
