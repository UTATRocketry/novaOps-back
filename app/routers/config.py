from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile

from app.context import AppContext
from app.deps import get_context

router = APIRouter(prefix="/api", tags=["Config"])


@router.get("/actuators", summary="List actuators", description="Return actuator definitions from loaded config.")
async def get_actuators(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_actuators()]


@router.get("/sensors", summary="List sensors", description="Return sensor definitions from loaded config.")
async def get_sensors(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_sensors()]


@router.get("/config", summary="Get full config", description="Return the active configuration as JSON.")
async def get_config(ctx: AppContext = Depends(get_context)) -> dict:
    return ctx.config_service.config.model_dump(by_alias=True)


@router.post("/config/upload", summary="Upload config file", description="Upload and activate a YAML config file.")
async def upload_config(file: UploadFile = File(...), ctx: AppContext = Depends(get_context)) -> dict:
    payload = await file.read()
    config = ctx.config_service.upload_config_bytes(payload)
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result


@router.put("/config", summary="Replace config", description="Replace the active configuration with supplied JSON payload.")
async def replace_config(config_payload: dict, ctx: AppContext = Depends(get_context)) -> dict:
    config = ctx.config_service.update_config(config_payload)
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result


@router.patch("/config", summary="Patch config", description="Shallow-merge a JSON payload into the active configuration.")
async def patch_config(config_payload: dict, ctx: AppContext = Depends(get_context)) -> dict:
    current = ctx.config_service.config.model_dump(by_alias=True)
    current.update(config_payload)
    config = ctx.config_service.update_config(current)
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result


@router.post("/config/reload", summary="Reload config", description="Reload config from disk path configured at startup.")
async def reload_config(ctx: AppContext = Depends(get_context)) -> dict:
    config = ctx.config_service.reload()
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result
