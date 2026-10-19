from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.context import AppContext
from app.deps import get_context

router = APIRouter(prefix="/api/config", tags=["Config"])


def _config_file(ctx: AppContext, name: str) -> Path:
    """Resolve a user-supplied config file name inside the config directory.

    Only bare *.yaml names are accepted, so a request can never read or activate
    a file elsewhere on disk (e.g. "../../somewhere.yaml").
    """
    if not name or Path(name).name != name or Path(name).suffix.lower() != ".yaml":
        raise HTTPException(status_code=400, detail="Expected a bare .yaml file name")
    return ctx.config_service.config_path.parent / name


def _client(ctx: AppContext, client_id: str | None) -> str | None:
    """Who made a config change, for the config history log."""
    if not client_id:
        return None
    return f"{client_id} ({ctx.role_service.get_role(client_id).name})"

@router.get("/", summary="Get full config", description="Return the active configuration as JSON.")
async def get_config(ctx: AppContext = Depends(get_context)) -> dict:
    return ctx.config_service.config.model_dump(by_alias=True)

@router.get("/actuators", summary="List actuators", description="Return actuator definitions from loaded config.")
async def get_actuators(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_actuators()]


@router.get("/sensors", summary="List sensors", description="Return sensor definitions from loaded config.")
async def get_sensors(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_sensors()]


@router.get("/procedures", summary="List procedures", description="Return checklist procedures from loaded config.")
async def get_procedures(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return [item.model_dump(by_alias=True) for item in ctx.config_service.config.all_procedures()]

@router.get("/list", summary="List config files", description="List YAML config files available in the config directory.")
async def list_configs(ctx: AppContext = Depends(get_context)) -> list[str]:
    config_dir = ctx.config_service.config_path.parent
    return [f.name for f in config_dir.glob("*.yaml")]

@router.post("/load", summary="Load a config file", description="Load and activate a YAML config file from disk.")
async def load_config(
    path: str,
    ctx: AppContext = Depends(get_context),
    x_client_id: str | None = Header(default=None),
) -> dict:
    config_path = _config_file(ctx, path)
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="Config file not found")
    config = ctx.config_service.set_config_path(config_path, "load", _client(ctx, x_client_id))
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result

@router.get("/download/{file_name}", summary="Download config file", description="Download a YAML config file by file name from the config directory.")
async def get_config_file(file_name: str, ctx: AppContext = Depends(get_context)) -> FileResponse:
    file_path = _config_file(ctx, file_name)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Config file not found")
    return FileResponse(file_path)

@router.post("/upload", summary="Upload config file", description="Upload and activate a YAML config file.")
async def upload_config(
    file: UploadFile = File(...),
    ctx: AppContext = Depends(get_context),
    x_client_id: str | None = Header(default=None),
) -> dict:
    file_name = Path(file.filename or "").name
    if not file_name:
        raise HTTPException(status_code=400, detail="File name is required")
    
    payload = await file.read()
    #config = ctx.config_service.upload_config_bytes(payload)
    config_dir = ctx.config_service.config_path.parent
    config_path = config_dir / file_name
    config_path.write_bytes(payload)
    config = ctx.config_service.set_config_path(config_path, "upload", _client(ctx, x_client_id))

    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result

@router.put("/update", summary="Replace config", description="Replace the active configuration with supplied JSON payload.")
async def replace_config(
    config_payload: dict,
    ctx: AppContext = Depends(get_context),
    x_client_id: str | None = Header(default=None),
) -> dict:
    config = ctx.config_service.update_config(config_payload, _client(ctx, x_client_id))
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result


@router.patch("/update", summary="Patch config", description="Shallow-merge a JSON payload into the active configuration.")
async def patch_config(
    config_payload: dict,
    ctx: AppContext = Depends(get_context),
    x_client_id: str | None = Header(default=None),
) -> dict:
    current = ctx.config_service.config.model_dump(by_alias=True)
    current.update(config_payload)
    config = ctx.config_service.update_config(current, _client(ctx, x_client_id))
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result


@router.post("/reload", summary="Reload config", description="Reload config from disk path configured at startup.")
async def reload_config(
    ctx: AppContext = Depends(get_context),
    x_client_id: str | None = Header(default=None),
) -> dict:
    config = ctx.config_service.reload()
    # The file may have been edited by hand on disk; snapshot what is now live.
    ctx.config_service.record_active("reload", _client(ctx, x_client_id))
    result = config.model_dump(by_alias=True)
    await ctx.broadcast({"type": "config_update", "config": result})
    return result
