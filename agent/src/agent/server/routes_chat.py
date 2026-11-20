"""Layer 2 系统快照、持久 SSE 事件总线、Conversation Tree 与对话控制路由。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Literal
from uuid import uuid4

from pydantic_ai.messages import UserContent
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.core.runtime import SuperAgentRuntime
from agent.db.tree_queries import TreeTopologyData
from agent.protocol import (
    ContextWindowSnapshotView,
    HealthView,
    InterruptResponse,
    LlmPoolStatusView,
    PoolStatsView,
    RecoveredBranchView,
    RuntimeReadyEvent,
    RuntimeReadyPayload,
    RuntimeSnapshotView,
    SendMessageReceipt,
    SendMessageRequest,
    SubscriptionUpdateRequest,
    TaskView,
    WorkspaceLockView,
    utc_rfc3339,
)
from agent.server.routes_tree import build_tree_view
from agent.server.state import (
    get_state,
    is_trusted_local,
    json_error,
    json_model,
    path_param,
)


def _pool_status_view(rt: SuperAgentRuntime) -> LlmPoolStatusView:
    ctrl = rt.llm_controller
    return LlmPoolStatusView(
        router_pool=PoolStatsView(
            current_in_flight=ctrl.router_stats.current_in_flight,
            peak_in_flight=ctrl.router_stats.peak_in_flight,
            total_requests=ctrl.router_stats.total_requests,
            rate_limit_retries=ctrl.router_stats.rate_limit_retries,
        ),
        role_pool=PoolStatsView(
            current_in_flight=ctrl.role_stats.current_in_flight,
            peak_in_flight=ctrl.role_stats.peak_in_flight,
            total_requests=ctrl.role_stats.total_requests,
            rate_limit_retries=ctrl.role_stats.rate_limit_retries,
        ),
        total_429_hits=ctrl.cooldown_gate.total_429_hits,
    )


async def health_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    return json_model(
        HealthView(
            pid=os.getpid(),
            model_name=rt.settings.model_name,
            trusted_local=is_trusted_local(request),
            active_conversation_id=rt.context_tree.active_conversation_id,
        )
    )


async def runtime_snapshot_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    topo = (
        await rt.protocol_store.read_tree_topology(include_role_branches=True)
        if rt.protocol_store is not None
        else TreeTopologyData(
            root_entry_id=rt.context_tree.root_entry_id,
            active_conversation_id=rt.context_tree.active_conversation_id,
            branches=[],
        )
    )
    tree_view = build_tree_view(topo, rt)
    active_cid = tree_view.active_conversation_id
    tasks: list[TaskView] = []
    if active_cid:
        rows = await rt.ticket_store.list_tickets(active_cid)
        tasks = [rt.scheduler.protocol_bridge.to_task_view(r) for r in rows]
    pending = (
        rt.interaction_broker.list_pending() if rt.interaction_broker is not None else []
    )
    locks = [
        WorkspaceLockView(
            work_path=s.work_path,
            exclusive_holder=s.exclusive_holder,
            shared_holders=list(s.shared_holders),
            queued_tasks=list(s.queued_tasks),
        )
        for s in await rt.scheduler.inspect_workspace_locks()
    ]
    ctx_snaps: dict[str, ContextWindowSnapshotView] = {}
    if rt.protocol_store is not None and active_cid:
        row = await rt.protocol_store.get_context_snapshot(active_cid)
        if row is not None:
            ctx_snaps[active_cid] = ContextWindowSnapshotView.model_validate_json(
                row.snapshot_json
            )
    recovery = [
        RecoveredBranchView(
            main_conversation_id=cid,
            interrupted_task_ids=intr,
            requeued_task_ids=req,
        )
        for cid, (intr, req) in rt.recovery_summary.items()
    ]
    return json_model(
        RuntimeSnapshotView(
            latest_seq=rt.event_bus.latest_seq if rt.event_bus is not None else 0,
            trusted_local=is_trusted_local(request),
            model_name=rt.settings.model_name,
            tree=tree_view,
            tasks=tasks,
            pending_interactions=pending,
            workspace_locks=locks,
            context_snapshots=ctx_snaps,
            pool_status=_pool_status_view(rt),
            recovery=recovery,
        )
    )


async def events_sse_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    if rt.event_bus is None:
        return json_error(
            request,
            status_code=503,
            code="EVENT_BUS_DISABLED",
            message="RuntimeEventBus is not enabled.",
        )
    cid = request.query_params.get("conversation_id")
    inc_tasks = request.query_params.get("include_task_streams", "false").lower() == "true"
    raw_after = request.headers.get("last-event-id") or request.query_params.get("after_seq")
    after_seq = int(raw_after) if raw_after and raw_after.isdigit() else None

    sub, replay = rt.event_bus.subscribe(
        conversation_id=cid,
        include_task_streams=inc_tasks,
        after_seq=after_seq,
    )
    ready_ev = RuntimeReadyEvent(
        seq=rt.event_bus.latest_seq,
        event_id=uuid4().hex,
        ts=utc_rfc3339(),
        payload=RuntimeReadyPayload(
            latest_seq=rt.event_bus.latest_seq,
            active_conversation_id=rt.context_tree.active_conversation_id,
            model_name=rt.settings.model_name,
            trusted_local=is_trusted_local(request),
            subscription_id=sub.subscription_id,
        ),
    )

    async def _stream() -> AsyncIterator[ServerSentEvent]:
        try:
            yield ServerSentEvent(
                data=ready_ev.model_dump_json(),
                event=ready_ev.type,
                id=str(ready_ev.seq),
            )
            for ev in replay:
                yield ServerSentEvent(
                    data=ev.model_dump_json(), event=ev.type, id=str(ev.seq)
                )
            async for ev in sub.events():
                yield ServerSentEvent(
                    data=ev.model_dump_json(), event=ev.type, id=str(ev.seq)
                )
        finally:
            if rt.event_bus is not None:
                rt.event_bus.unsubscribe(sub.subscription_id)

    return EventSourceResponse(_stream(), ping=15)


async def update_subscriptions_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    if rt.event_bus is None:
        return json_error(
            request, status_code=503, code="EVENT_BUS_DISABLED", message="EventBus disabled."
        )
    body = SubscriptionUpdateRequest.model_validate_json(await request.body())
    ok = rt.event_bus.update_subscription(body.subscription_id, task_ids=body.task_ids)
    if not ok:
        return json_error(
            request,
            status_code=404,
            code="NOT_FOUND",
            message=f"Subscription {body.subscription_id!r} not found.",
        )
    return JSONResponse({"ok": True, "task_ids": body.task_ids})


async def send_message_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    state = get_state(request)
    rt = state.runtime
    body = SendMessageRequest.model_validate_json(await request.body())
    cached = state.idempotency_cache.get(body.client_message_id)
    if cached is not None:
        return json_model(cached, status_code=202)

    handles: list[str] = []
    media_contents: list[UserContent] = []
    for att_id in body.attachment_ids:
        ref = rt.attachment_registry.get_attachment(att_id)
        if ref is None:
            return json_error(
                request,
                status_code=404,
                code="NOT_FOUND",
                message=f"Attachment {att_id!r} not found.",
            )
        handles.append(ref.to_reference_handle())
        should_inline = body.attachment_mode == "inline" or (
            body.attachment_mode == "auto"
            and (
                ref.kind in ("image", "audio", "video")
                or (ref.kind == "document" and (ref.page_count or 0) <= 20)
            )
        )
        if should_inline:
            media_contents.append(rt.attachment_registry.to_pydantic_ai_content(att_id))

    full_text = f"{body.text}\n\n" + "\n".join(handles) if handles else body.text
    prompt: str | list[UserContent] = (
        [full_text, *media_contents] if media_contents else full_text
    )

    if rt.router.is_running(cid) and body.delivery_mode == "steer":
        _ = rt.router.steer_conversation(cid, full_text)
        receipt = SendMessageReceipt(
            client_message_id=body.client_message_id,
            run_id=f"steer_{uuid4().hex[:10]}",
            resolved_conversation_id=cid,
            delivery_mode="steered",
            routing_action="PINNED",
        )
        state.idempotency_cache[body.client_message_id] = receipt
        return json_model(receipt, status_code=202)

    run_id = f"run_{uuid4().hex[:12]}"
    delivery_label: Literal["started", "steered", "queued", "interrupted_and_started"] = (
        "interrupted_and_started"
        if body.delivery_mode == "interrupt_and_send" and rt.router.is_running(cid)
        else ("queued" if body.delivery_mode == "queue" and rt.router.is_running(cid) else "started")
    )

    async def _bg_run() -> None:
        _ = await rt.router.handle_user_message(
            prompt,
            conversation_id=cid,
            work_path=body.work_path,
            routing_mode=body.routing_mode,
            delivery_mode=body.delivery_mode,
            run_id=run_id,
        )

    bg_task = asyncio.create_task(_bg_run(), name=f"router-run-{run_id}")
    state.background_runs[run_id] = bg_task
    bg_task.add_done_callback(lambda _t: state.background_runs.pop(run_id, None))

    receipt = SendMessageReceipt(
        client_message_id=body.client_message_id,
        run_id=run_id,
        resolved_conversation_id=cid,
        delivery_mode=delivery_label,
        routing_action="PINNED" if body.routing_mode == "pinned" else "CONTINUE",
    )
    state.idempotency_cache[body.client_message_id] = receipt
    return json_model(receipt, status_code=202)


async def interrupt_conversation_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    interrupted, run_id, head_id = await rt.router.interrupt_conversation(cid)
    return json_model(
        InterruptResponse(
            conversation_id=cid,
            interrupted=interrupted,
            run_id=run_id,
            head_entry_id=head_id,
        )
    )


def get_chat_routes() -> list[Route]:
    return [
        Route("/v1/health", health_endpoint, methods=["GET"]),
        Route("/v1/runtime/snapshot", runtime_snapshot_endpoint, methods=["GET"]),
        Route("/v1/events", events_sse_endpoint, methods=["GET"]),
        Route("/v1/events/subscriptions", update_subscriptions_endpoint, methods=["POST"]),
        Route("/v1/conversations/{cid}/messages", send_message_endpoint, methods=["POST"]),
        Route("/v1/conversations/{cid}/interrupt", interrupt_conversation_endpoint, methods=["POST"]),
    ]
