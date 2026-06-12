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
