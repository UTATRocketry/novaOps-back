from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.deps import get_context

router = APIRouter()

_base_dir = Path(__file__).resolve().parent.parent.parent
_templates_dir = _base_dir / "app" / "templates"
_static_dir = _base_dir / "app" / "static"

templates = Jinja2Templates(directory=str(_templates_dir)) if _templates_dir.exists() else None


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if templates is None:
        return HTMLResponse("<h1>NovaOps Backend</h1><p>API is running.</p>")
    return templates.TemplateResponse(request, "index.html")


@router.get("/view-json")
async def get_data_ui():
    file_path = _static_dir / "wsJSON.html"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="UI file not found")
    return FileResponse(file_path)


@router.get("/view-mqtt")
async def get_mqtt_ui():
    file_path = _static_dir / "mqttTopics.html"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="UI file not found")
    return FileResponse(file_path)


@router.get("/health", tags=["System"], summary="Health check", description="Simple liveness endpoint.")
async def health() -> dict[str, str]:
    return {"status": "ok"}
