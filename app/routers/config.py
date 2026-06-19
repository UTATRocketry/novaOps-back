from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.context import AppContext
from app.deps import get_context

router = APIRouter(prefix="/api", tags=["Config"])


@router.get("/actuators", summary="List actuators", description="Return actuator definitions from loaded config.")
async def get_actuators(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_actuators()]


@router.get("/sensors", summary="List sensors", description="Return sensor definitions from loaded config.")
async def get_sensors(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_sensors()]


@router.get("/procedures", summary="List procedures", description="Return checklist procedures from loaded config.")
async def get_procedures(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_procedures()]


@router.get("/config", summary="Get full config", description="Return the active configuration as JSON.")
async def get_config(ctx: AppContext = Depends(get_context)) -> dict:
    return ctx.config_service.config.model_dump(by_alias=True)


@router.get("/config/list", summary="List config files", description="List YAML config files available in the config directory.")
async def list_configs(ctx: AppContext = Depends(get_context)) -> list[str]:
    config_dir = ctx.config_service.config_path.parent
    return [f.name for f in config_dir.glob("*.yaml")]

@router.post("/config/load", summary="Load a config file", description="Load and activate a YAML config file from disk.")
async def load_config(path: str, ctx: AppContext = Depends(get_context)) -> dict:
    config_path = ctx.config_service.config_path.parent / path
    config = ctx.config_service.set_config_path(config_path)
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result

@router.get("/config/download/{file_name}", summary="Download config file", description="Download a YAML config file by file name from the config directory.")
async def get_config_file(file_name: str, ctx: AppContext = Depends(get_context)) -> FileResponse:
    config_dir = ctx.config_service.config_path.parent
    file_path = config_dir / file_name
    if not file_path.exists() or file_path.suffix.lower() != ".yaml":
        raise HTTPException(status_code=404, detail="Config file not found")
    return FileResponse(file_path)

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
