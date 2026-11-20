"""Layer 2 Context Window 9 桶快照与三层 Token 用量遥测路由。"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent.core.budget import ROUTER_BUDGET_POLICY
from agent.protocol import (
    ContextWindowSnapshotView,
    RoleTaskUsageItemView,
    RunUsageView,
    build_tree_usage_summary,
    to_run_usage_view,
)
from agent.server.state import get_state, json_error, json_model, path_param


async def get_context_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    if rt.protocol_store is None:
        return json_error(
            request,
            status_code=503,
            code="PROTOCOL_STORE_DISABLED",
            message="Protocol accounting store is not enabled.",
        )
    row = await rt.protocol_store.get_context_snapshot(cid)
    if row is None:
        return json_error(
            request,
            status_code=404,
            code="NOT_FOUND",
            message=f"No context snapshot recorded yet for {cid!r}.",
        )
    snap = ContextWindowSnapshotView.model_validate_json(row.snapshot_json)
    return json_model(snap)


async def get_usage_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    rec = await rt.store.read_record(cid)
    if rec is None:
        return json_error(
            request, status_code=404, code="NOT_FOUND", message=f"Conversation {cid!r} not found."
        )
    main_usage = to_run_usage_view(rec.usage, budget_policy=ROUTER_BUDGET_POLICY)
    tickets = await rt.ticket_store.list_tickets(cid)
    role_items: list[RoleTaskUsageItemView] = []
    for t in tickets:
        if t.role_conversation_id:
            r_rec = await rt.store.read_record(t.role_conversation_id)
            r_usage = to_run_usage_view(r_rec.usage) if r_rec is not None else RunUsageView()
        else:
            r_usage = RunUsageView()
        role_items.append(
            RoleTaskUsageItemView(task_id=t.task_id, role_name=t.role_name, usage=r_usage)
        )
    aux_rows = (
        await rt.protocol_store.list_aux_usage(cid) if rt.protocol_store is not None else []
    )
    summary = build_tree_usage_summary(
        conversation_id=cid,
        main_usage=main_usage,
        role_items=role_items,
        aux_rows=aux_rows,
    )
    return json_model(summary)


def get_telemetry_routes() -> list[Route]:
    return [
        Route("/v1/conversations/{cid}/context", get_context_endpoint, methods=["GET"]),
        Route("/v1/conversations/{cid}/usage", get_usage_endpoint, methods=["GET"]),
    ]
