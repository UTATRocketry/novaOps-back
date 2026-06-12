
from fastapi import APIRouter, Depends, HTTPException
from typing import Any

from app.context import AppContext
from app.deps import get_context

router = APIRouter(prefix="/api", tags=["Diagram"])


_latest_layout: dict[str, Any] | None = None


@router.get("/diagram")
async def get_diagram():
    if _latest_layout is None:
        raise HTTPException(status_code=404, detail="No diagram available")
    return {"layout": _latest_layout}

@router.put("/diagram")
async def update_diagram(layout: dict[str, Any], ctx: AppContext = Depends(get_context)):
    global _latest_layout
    _latest_layout = layout
    await ctx.broadcast({"type": "diagram_update", "layout": _latest_layout})
    return {"message": "Diagram layout updated"}