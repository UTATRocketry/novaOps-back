from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.models import CommandPayload
from app.services.role_service import ClientRole

router = APIRouter()


async def _handle_role_request_ws(websocket: WebSocket, ctx, caller_client_id: str, msg: dict) -> None:
    raw_role = msg.get("role", "")
    target_id = msg.get("target_client_id") or caller_client_id
    password = msg.get("password")

    try:
        new_role = ClientRole.from_str(raw_role)
    except ValueError as exc:
        await ctx.ws_manager.send_json(websocket, {"type": "error", "detail": str(exc)})
        return

    caller_role = ctx.role_service.get_role_by_ws(websocket)
    is_self = target_id == caller_client_id

    if not is_self and caller_role < ClientRole.admin:
        await ctx.ws_manager.send_json(
            websocket,
            {"type": "error", "detail": "Only admin can assign roles to other clients"},
        )
        return

    if new_role == ClientRole.admin:
        if ctx.role_service.get_admin_password() is None:
            await ctx.ws_manager.send_json(
                websocket,
                {"type": "error", "detail": "Admin role is disabled (NOVA_ADMIN_PASSWORD not set)"},
            )
            return
        if not ctx.role_service.verify_admin_password(password):
            await ctx.ws_manager.send_json(websocket, {"type": "error", "detail": "Invalid admin password"})
            return

    ok = await ctx.role_service.assign_role(target_id, new_role)
    if not ok:
        await ctx.ws_manager.send_json(
            websocket,
            {"type": "error", "detail": f"Client '{target_id}' not found"},
        )


async def _handle_websocket(websocket: WebSocket, ctx) -> None:
    client_id = await ctx.ws_manager.connect(websocket)
    initial_role = ctx.role_service.on_connect(client_id)

    await ctx.ws_manager.send_json(
        websocket,
        {"type": "session", "role": initial_role.name, "client_id": client_id},
    )
    await ctx.ws_manager.send_json(
        websocket,
        {
            "type": "snapshot",
            "role": initial_role.name,
            "client_id": client_id,
            "actuator_states": ctx.runtime.actuator_states,
        },
    )

    if ctx.runtime.latest_sensors:
        await ctx.ws_manager.send_json(websocket, {"type": "parsed_data", "sensors": ctx.runtime.latest_sensors})
    if ctx.runtime.latest_engine_data:
        await ctx.ws_manager.send_json(websocket, {"type": "engine_data", "data": ctx.runtime.latest_engine_data})
    if ctx.runtime.latest_flight_data:
        await ctx.ws_manager.send_json(websocket, {"type": "flight_data", "data": ctx.runtime.latest_flight_data})
    if ctx.runtime.latest_events:
        await ctx.ws_manager.send_json(websocket, {"type": "flight_events", "events": ctx.runtime.latest_events})
    await ctx.ws_manager.send_json(websocket, {"type": "lockout", "state": ctx.runtime.lockout_state})

    try:
        while True:
            payload = await websocket.receive_json()
            msg_type = payload.get("type")

            if msg_type == "console":
                console_payload = payload.get("payload")
                ctx.mqtt_service.publish_console(console_payload if isinstance(console_payload, dict) else payload)
                continue

            if msg_type == "role_request":
                await _handle_role_request_ws(websocket, ctx, client_id, payload)
                continue

            role = ctx.role_service.get_role_by_ws(websocket)
            try:
                command = CommandPayload.model_validate(payload)
                ctx.role_service.check_command_role(role, command.name, command.state, ctx.config_service.config)
                await ctx.command_service.apply_command(command)
            except ValueError as exc:
                await ctx.ws_manager.send_json(websocket, {"type": "error", "detail": str(exc)})

    except WebSocketDisconnect:
        removed_id = await ctx.ws_manager.disconnect(websocket)
        if removed_id:
            await ctx.role_service.on_disconnect(removed_id)


@router.websocket("/ws_basic")
async def websocket_basic_endpoint(websocket: WebSocket) -> None:
    ctx = websocket.app.state.ctx
    await _handle_websocket(websocket, ctx)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    ctx = websocket.app.state.ctx
    await _handle_websocket(websocket, ctx)
