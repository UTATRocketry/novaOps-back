from __future__ import annotations

from fastapi import APIRouter, Depends

from app.context import AppContext
from app.deps import get_context
from app.models import FlagPayload

router = APIRouter(prefix="/api/lock", tags=["novaLock"])


@router.get("/lock", summary="Get lock state", description="Return current lock engaged state.")
async def get_lock_state(ctx: AppContext = Depends(get_context)) -> dict:
    return {"engaged": ctx.runtime.lock_engaged}

@router.post("/lock", summary="Set lock state", description="Engage or disengage the novaLock.")
async def set_lock_state(payload: FlagPayload, ctx: AppContext = Depends(get_context)) -> dict:
    ctx.runtime.lock_engaged = payload.enabled
    return {"engaged": ctx.runtime.lock_engaged}