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


def test_first_client_is_operator_then_viewer() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()

        role1 = await manager.connect(ws1)
        role2 = await manager.connect(ws2)

        assert role1 == "operator"
        assert role2 == "viewer"

    asyncio.run(run())


def test_operator_reassigned_on_disconnect() -> None:
    async def run() -> None:
        manager = WebSocketManager()
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()

        await manager.connect(ws1)
        await manager.connect(ws2)

        await manager.disconnect(ws1)

        assert any(message.get("type") == "session" and message.get("role") == "operator" for message in ws2.messages)

    asyncio.run(run())
