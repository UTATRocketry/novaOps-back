from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.context import AppContext
from app.deps import get_context

router = APIRouter(prefix="/api/nodes", tags=["Nodes"])


def _node_view(ctx: AppContext, node: str) -> dict:
    runtime = ctx.runtime
    return {
        "info": runtime.node_stats_info.get(node),
        "stats": runtime.node_stats.get(node),
        "state": runtime.node_state.get(node),
    }


def _known_nodes(ctx: AppContext) -> list[str]:
    runtime = ctx.runtime
    return sorted({*runtime.node_stats_info, *runtime.node_stats, *runtime.node_state})


@router.get(
    "",
    summary="Runtime stats and actuator state of every node",
    description=(
        "The latest `nova/stats/<node>/info`, `nova/stats/<node>` and `nova/state/<node>` "
        "message of each node (novaGround, novaThermo, novaSim) that has published one. "
        "Each part is null until that node has sent it. Stats are dropped when a node "
        "stops publishing; info and state are retained messages and are kept, so check "
        "the `time_ms` in `state` and `started_ms` in `info`."
    ),
)
async def get_nodes(ctx: AppContext = Depends(get_context)) -> dict:
    return {"nodes": {node: _node_view(ctx, node) for node in _known_nodes(ctx)}}


@router.get("/{node}", summary="Runtime stats and actuator state of one node")
async def get_node(node: str, ctx: AppContext = Depends(get_context)) -> dict:
    if node not in _known_nodes(ctx):
        raise HTTPException(status_code=404, detail=f"No stats or state received from '{node}'")
    return _node_view(ctx, node)
