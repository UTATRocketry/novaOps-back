from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.context import AppContext
from app.deps import get_context
from app.services.mqtt_service import InvalidTopicFilter
from app.services.role_service import ClientRole

router = APIRouter(prefix="/api/mqtt", tags=["MQTT"])


class TopicPayload(BaseModel):
    topic: str


def _require_operator(ctx: AppContext, x_client_id: str | None) -> None:
    if ctx.role_service.resolve_caller_role(x_client_id) < ClientRole.operator:
        raise HTTPException(
            status_code=403,
            detail="Insufficient role: operator or admin required to change MQTT subscriptions",
        )


@router.get(
    "/topics",
    summary="List extra MQTT subscriptions",
    description="Topic filters subscribed at runtime, in addition to the built-in topics.",
)
async def get_topics(ctx: AppContext = Depends(get_context)) -> dict:
    return {"topics": ctx.mqtt_service.extra_topics}


@router.post(
    "/topics",
    summary="Subscribe to an extra MQTT topic",
    description=(
        "Subscribe the backend to a topic filter (`+` and `#` wildcards allowed). Messages "
        "are only relayed to WebSocket clients as `mqtt_message`; no other handling. "
        "Not persisted: subscriptions are cleared when the backend restarts. "
        "Requires operator or admin role."
    ),
)
async def post_topic(
    payload: TopicPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _require_operator(ctx, x_client_id)
    try:
        ctx.mqtt_service.subscribe_topic(payload.topic)
    except InvalidTopicFilter as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"topics": ctx.mqtt_service.extra_topics}


@router.post(
    "/topics/unsubscribe",
    summary="Unsubscribe from an extra MQTT topic",
    description="Requires operator or admin role. Built-in topics cannot be removed.",
)
async def post_unsubscribe_topic(
    payload: TopicPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _require_operator(ctx, x_client_id)
    if not ctx.mqtt_service.unsubscribe_topic(payload.topic):
        raise HTTPException(status_code=404, detail=f"Not subscribed to '{payload.topic}'")
    return {"topics": ctx.mqtt_service.extra_topics}
