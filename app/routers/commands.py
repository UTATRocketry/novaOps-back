from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.context import AppContext
from app.deps import get_context
from app.models import (
    CommandPayload,
    DirectRelayPayload,
    DirectServoPayload,
    FasBuzzerPayload,
    FasSdPayload,
    SystemCommandPayload,
)
from app.services.role_service import ClientRole

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
    "/console/command",
    summary="Send a FAS console command",
    description=(
        "Publish a console control/TX command to the device command topic so the "
        "FAS bridge acts on it. Supports actions: start, stop, list_ports, "
        "configure, tx. Requires operator or admin role (raw frame TX is "
        "powerful). Output is delivered back over the WebSocket console stream."
    ),
)
async def post_console_command(
    payload: dict,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(
            status_code=403,
            detail="Insufficient role: operator or admin required for console commands",
        )
    action = str(payload.get("action", "")).lower()
    if action not in {"start", "stop", "list_ports", "configure", "tx"}:
        raise HTTPException(status_code=400, detail=f"Unknown console action '{action}'")
    ctx.mqtt_service.publish_console_command(payload)
    return {"published": True, "action": action}


@router.post(
    "/direct/relay",
    summary="Direct relay control",
    description=(
        "Set a relay channel directly by number, bypassing config-based name resolution. "
        "Requires operator or admin role. state: 0=off, 1=on."
    ),
)
async def post_direct_relay(
    payload: DirectRelayPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_direct_relay(payload)
    return {"published_commands": commands}


@router.post(
    "/direct/servo",
    summary="Direct servo control",
    description=(
        "Set a servo/PWM channel directly by number and pulse width (µs), "
        "bypassing config-based name resolution. "
        "Requires operator or admin role. pulse_us=0 disables the output."
    ),
)
async def post_direct_servo(
    payload: DirectServoPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_direct_servo(payload)
    return {"published_commands": commands}


@router.post(
    "/fas/buzzer",
    summary="Control the FMC buzzer",
    description=(
        "Drive the FAS FMC buzzer. action: begin (start a melody), note (append "
        "a freq/duration note), play (play melody by idx), stop (silence). "
        "Requires operator or admin role."
    ),
)
async def post_fas_buzzer(
    payload: FasBuzzerPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    try:
        commands = ctx.command_service.apply_fas_buzzer(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"published_commands": commands}


@router.post(
    "/fas/sd",
    summary="Control the FMC SD-card logger",
    description=(
        "Control the FAS FMC SD-card logger. action: set_rate (set the log "
        "decimation divisor, 1 = full rate) or clear (reformat the card). "
        "Requires operator or admin role."
    ),
)
async def post_fas_sd(
    payload: FasSdPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_fas_sd(payload)
    return {"published_commands": commands}


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
