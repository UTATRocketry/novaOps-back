from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from app.context import AppContext
from app.services.role_service import ClientRole


def get_context(request: Request) -> AppContext:
    return request.app.state.ctx


def require_role(min_role: ClientRole):
    """FastAPI dependency factory. Use as: dependencies=[require_role(ClientRole.admin)]"""
    def dependency(
        x_client_id: str | None = Header(default=None),
        ctx: AppContext = Depends(get_context),
    ) -> None:
        role = ctx.role_service.get_role(x_client_id)
        if role < min_role:
            raise HTTPException(status_code=403, detail=f"Requires {min_role.name} role or higher")
    return Depends(dependency)
