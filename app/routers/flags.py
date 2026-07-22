from __future__ import annotations

from fastapi import APIRouter, Depends

from app.context import AppContext
from app.deps import get_context
from app.models import FlagPayload

router = APIRouter(prefix="/api/flags", tags=["Flags"])


@router.get("/calibration", summary="Get calibration flag", description="Return current calibration enabled state.")
async def get_calibration_flag(ctx: AppContext = Depends(get_context)) -> dict:
    return {"enabled": ctx.runtime.calibration_enabled}


@router.post("/calibration", summary="Set calibration flag", description="Enable or disable calibrated sensor output.")
async def set_calibration_flag(payload: FlagPayload, ctx: AppContext = Depends(get_context)) -> dict:
    ctx.runtime.calibration_enabled = payload.enabled
    return {"enabled": ctx.runtime.calibration_enabled}


@router.get("/data-saving", summary="Get data saving flag", description="Return current data saving enabled state.")
async def get_data_saving_flag(ctx: AppContext = Depends(get_context)) -> dict:
    return {"enabled": ctx.runtime.data_saving_enabled}


@router.post("/data-saving", summary="Set data saving flag", description="Start/stop logger commands over MQTT.")
async def set_data_saving_flag(payload: FlagPayload, ctx: AppContext = Depends(get_context)) -> dict:
    ctx.runtime.data_saving_enabled = payload.enabled
    ctx.mqtt_service.publish_data_saving(payload.enabled)
    return {"enabled": ctx.runtime.data_saving_enabled}


@router.get(
    "/lockout",
    summary="Get novaLock lockout state",
    description="Return the current safety lockout state (the gate for hazardous commands / RAB arming).",
)
async def get_lockout_flag(ctx: AppContext = Depends(get_context)) -> dict:
    return {"locked": ctx.runtime.lockout_is_locked, "state": ctx.runtime.lockout_state}


@router.post(
    "/lockout",
    summary="Override the novaLock lockout state (test/manual)",
    description=(
        "Virtually engage or disengage the safety lockout without a running "
        "novaLock: enabled=true locks (hazardous commands and RAB arming are "
        "blocked), enabled=false unlocks. Broadcasts the same `lockout` WebSocket "
        "message novaLock would, so connected clients update immediately. Note: a "
        "live novaLock republishes its own state over MQTT and will override this."
    ),
)
async def set_lockout_flag(payload: FlagPayload, ctx: AppContext = Depends(get_context)) -> dict:
    ctx.runtime.lockout_state = "locked" if payload.enabled else "unlocked"
    await ctx.broadcast({"type": "lockout", "state": ctx.runtime.lockout_state})
    return {"locked": ctx.runtime.lockout_is_locked, "state": ctx.runtime.lockout_state}
