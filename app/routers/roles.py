from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.context import AppContext
from app.deps import get_context
from app.models import RoleAssignPayload
from app.services.role_service import ClientRole

router = APIRouter(prefix="/api/roles", tags=["Roles"])


@router.get(
    "",
    summary="List connected clients",
    description="Return all connected WebSocket clients and their current roles.",
)
async def list_roles(ctx: AppContext = Depends(get_context)) -> list[dict]:
    return ctx.role_service.list_clients()


@router.post(
    "",
    summary="Assign role",
    description=(
        "Assign a role to a client. "
        "Omit `target_client_id` to change your own role. "
        "Changing another client's role requires the caller to be admin. "
        "The `admin` role requires the correct password in the `password` field. "
        "The caller is identified by the `X-Client-Id` header."
    ),
)
async def assign_role(
    payload: RoleAssignPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    role_svc = ctx.role_service

    try:
        new_role = ClientRole.from_str(payload.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    caller_role = role_svc.resolve_caller_role(x_client_id)
    target_id = payload.target_client_id or x_client_id

    if target_id is None:
        raise HTTPException(
            status_code=400,
            detail="Cannot determine target client: provide X-Client-Id header or target_client_id",
        )

    is_self = target_id == x_client_id or payload.target_client_id is None

    if not is_self and caller_role < ClientRole.admin:
        raise HTTPException(status_code=403, detail="Only admin can assign roles to other clients")

    if new_role == ClientRole.admin:
        if role_svc.get_admin_password() is None:
            raise HTTPException(status_code=503, detail="Admin role is disabled (NOVA_ADMIN_PASSWORD not set)")
        if not role_svc.verify_admin_password(payload.password):
            raise HTTPException(status_code=403, detail="Invalid admin password")

    ok = await ctx.role_service.assign_role(target_id, new_role)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Client '{target_id}' not found")

    return {"client_id": target_id, "role": new_role.name}
