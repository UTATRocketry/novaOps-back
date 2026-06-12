from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.context import AppContext
from app.routers import commands, config, data, flags, roles, views, diagram
from app.routers import websocket as ws_router


def configure_logging(base_dir: Path) -> None:
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_dir / "server.log", maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)


def create_app() -> FastAPI:
    base_dir = Path(__file__).resolve().parent.parent
    configure_logging(base_dir)

    ctx = AppContext(
        config_path=base_dir / "config" / "system.yaml",
        data_dir=base_dir / "data",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.startup()
        yield
        await ctx.shutdown()

    openapi_tags = [
        {"name": "System", "description": "Health and service status endpoints."},
        {"name": "Config", "description": "Load, view, and update YAML-backed system configuration."},
        {"name": "Flags", "description": "Runtime feature flags such as calibration and data saving."},
        {"name": "Data", "description": "CSV data file listing and download endpoints."},
        {"name": "Commands", "description": "Actuator command translation and MQTT publish endpoints."},
        {"name": "Roles", "description": "Client role assignment and management."},
        {"name": "Diagram", "description": "Endpoints for managing and retrieving frontend diagram layouts."},
    ]

    app = FastAPI(
        title="NovaOps Backend",
        version="2.0.0",
        summary="FastAPI backend for NovaOps control system",
        description=(
            "Provides configuration-driven parsing, MQTT bridge logic, actuator command translation, "
            "and websocket streaming for NovaOps clients."
        ),
        openapi_tags=openapi_tags,
        lifespan=lifespan,
    )

    app.state.ctx = ctx

    static_dir = base_dir / "app" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(views.router)
    app.include_router(config.router)
    app.include_router(flags.router)
    app.include_router(data.router)
    app.include_router(roles.router)
    app.include_router(commands.router)
    app.include_router(diagram.router)
    app.include_router(ws_router.router)

    return app


app = create_app()
