from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.context import AppContext
from app.deps import get_context
from app.models import CommandPayload, SystemCommandPayload

router = APIRouter(prefix="/api", tags=["Commands"])


@router.post(
    "/commands",
    summary="Send actuator command",
    description=(
        "Translate and publish a client actuator command over MQTT. "
        "Role is checked via `X-Client-Id` header: viewers are blocked; "
        "pad role is restricted to safety-critical commands."
    ),
)
async def post_command(
    payload: CommandPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    try:
        ctx.role_service.check_command_role(caller_role, payload.name, payload.state, ctx.config_service.config)
        commands = await ctx.command_service.apply_command(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"published_commands": commands}


@router.post("/console", summary="Send raw console payload", description="Publish an unformatted console payload to nova/console.")
async def post_console(payload: dict, ctx: AppContext = Depends(get_context)) -> dict:
    ctx.mqtt_service.publish_console(payload)
    return {"published": True}


@router.post(
    "/system-commands",
    summary="Send system command",
    description=(
        "Dispatch a Commands-section command. "
        "Role is checked via `X-Client-Id` header."
    ),
)
async def post_system_command(
    payload: SystemCommandPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    try:
        ctx.role_service.check_command_role(caller_role, payload.name, payload.state, ctx.config_service.config)
        return ctx.command_service.apply_system_command(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
