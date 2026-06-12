import asyncio

from app.websocket_manager import WebSocketManager


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.messages: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def test_connect_accepts_socket_and_returns_uuid() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws = FakeWebSocket()
        client_id = await manager.connect(ws)
        assert ws.accepted
        assert isinstance(client_id, str) and len(client_id) == 36

    asyncio.run(run())


def test_disconnect_returns_client_id() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws = FakeWebSocket()
        client_id = await manager.connect(ws)
        removed = await manager.disconnect(ws)
        assert removed == client_id

    asyncio.run(run())


def test_disconnect_unknown_socket_returns_none() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws = FakeWebSocket()
        removed = await manager.disconnect(ws)
        assert removed is None

    asyncio.run(run())


def test_broadcast_sends_to_all_clients() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()
        await manager.connect(ws1)
        await manager.connect(ws2)

        await manager.broadcast({"type": "ping"})

        assert {"type": "ping"} in ws1.messages
        assert {"type": "ping"} in ws2.messages

    asyncio.run(run())


def test_broadcast_removes_stale_clients_and_returns_ids() -> None:
    class FailingWebSocket(FakeWebSocket):
        async def send_json(self, payload: dict) -> None:
            raise RuntimeError("disconnected")

    async def run() -> None:
        manager = WebSocketManager()
        ws_ok = FakeWebSocket()
        ws_bad = FailingWebSocket()
        await manager.connect(ws_ok)
        bad_id = await manager.connect(ws_bad)

        removed_ids = await manager.broadcast({"type": "ping"})

        assert bad_id in removed_ids
        assert manager.get_client_id(ws_bad) is None
        assert {"type": "ping"} in ws_ok.messages

    asyncio.run(run())


def test_get_client_id_reverse_lookup() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws = FakeWebSocket()
        client_id = await manager.connect(ws)
        assert manager.get_client_id(ws) == client_id

    asyncio.run(run())


def test_list_client_ids() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()
        cid1 = await manager.connect(ws1)
        cid2 = await manager.connect(ws2)
        ids = manager.list_client_ids()
        assert set(ids) == {cid1, cid2}

    asyncio.run(run())
