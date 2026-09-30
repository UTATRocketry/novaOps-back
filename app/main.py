from __future__ import annotations

import logging
import os
import shutil
import subprocess
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


def seed_config_dir(config_dir: Path, defaults_dir: Path) -> None:
    """Give a fresh station config directory the repo's published defaults.

    Only ever fills an EMPTY directory: once a station has its own config, a
    code update must never overwrite its calibrations.
    """
    if config_dir.resolve() == defaults_dir.resolve():
        return
    config_dir.mkdir(parents=True, exist_ok=True)
    if any(config_dir.glob("*.yaml")):
        return
    for src in defaults_dir.glob("*.yaml"):
        shutil.copy2(src, config_dir / src.name)
        logging.getLogger(__name__).info("Seeded %s from repo defaults", config_dir / src.name)


def software_info(base_dir: Path) -> dict[str, str | None]:
    """What produced a recording: the release (set by ops/Nova.ps1) and commit."""
    commit = None
    try:
        out = subprocess.run(
            ["git", "-C", str(base_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0:
            commit = out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {"release": os.getenv("NOVA_RELEASE") or None, "backend_commit": commit}


def create_app() -> FastAPI:
    base_dir = Path(__file__).resolve().parent.parent
    configure_logging(base_dir)

    # Station state lives OUTSIDE the code checkout when ops/Nova.ps1 runs us
    # (C:\Nova\config\<env>, C:\Nova\data\<env>), so checking out a different
    # version never touches live calibrations or recordings. Unset, everything
    # falls back to the repo's own folders, as before.
    defaults_dir = base_dir / "config"
    config_dir = Path(os.getenv("NOVA_CONFIG_DIR") or defaults_dir)
    data_dir = Path(os.getenv("NOVA_DATA_DIR") or base_dir / "data")
    history_dir = Path(os.getenv("NOVA_CONFIG_HISTORY_DIR") or config_dir / "history")
    seed_config_dir(config_dir, defaults_dir)

    ctx = AppContext(
        config_path=config_dir / "system.yaml",
        data_dir=data_dir,
        history_dir=history_dir,
        env=os.getenv("NOVA_ENV", ""),
        software=software_info(base_dir),
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
