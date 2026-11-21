"""Layer 2 Conversation Tree 双层懒加载与分支操作路由。"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.core.budget import ROUTER_BUDGET_POLICY
from agent.core.runtime import SuperAgentRuntime
from agent.db.tree_queries import EntryPageData, TreeTopologyData
from agent.protocol import (
    BranchModeLiteral,
    BranchView,
    CreateConversationRequest,
    EntryPageView,
    EntryPartView,
    EntryView,
    ForkConversationRequest,
    PatchConversationRequest,
    RunTurnView,
    TreeBranchCreatedPayload,
    TreeBranchSwitchedPayload,
    TreeBranchUpdatedPayload,
    TreeView,
    TurnPageView,
    TurnStepView,
    to_run_usage_view,
    utc_rfc3339,
)
from agent.server.state import get_state, json_error, json_model, path_param


def build_tree_view(topo: TreeTopologyData, rt: SuperAgentRuntime) -> TreeView:
    pending_all = (
        rt.interaction_broker.list_pending() if rt.interaction_broker is not None else []
    )
    branches: list[BranchView] = []
    for rec in topo.branches:
        row = rec.row
        mode: BranchModeLiteral = (
            row.branch_mode
            if row.branch_mode in ("root", "root_only", "handoff", "full_fork")
            else "root_only"
        )
        is_role = rec.task_id is not None
        if is_role:
            pending_cnt = sum(
                1
                for p in pending_all
                if p.conversation_id == row.parent_conversation_id and p.task_id == rec.task_id
            )
            running = rec.task_status == "running"
            budget_policy = None
        else:
            pending_cnt = sum(
                1
                for p in pending_all
                if p.conversation_id == row.conversation_id and p.task_id is None
            )
            running = rt.router.is_running(row.conversation_id)
            budget_policy = ROUTER_BUDGET_POLICY

        branches.append(
            BranchView(
                conversation_id=row.conversation_id,
                scope=row.scope or "main",
                parent_conversation_id=row.parent_conversation_id,
                role_name=row.role_name,
                task_id=rec.task_id,
                target_title=row.target_title,
                work_path=row.work_path,
                branch_mode=mode,
                forked_from=row.forked_from,
                forked_at=row.forked_at,
                head_entry_id=row.head_entry_id,
                status=row.status,
                is_running=running,
                message_count=rec.message_count,
                active_task_count=rec.active_task_count,
                pending_interaction_count=pending_cnt,
                usage_summary=to_run_usage_view(rec.usage, budget_policy=budget_policy),
                updated_at=row.updated_at,
            )
        )
    return TreeView(
        root_entry_id=topo.root_entry_id,
        active_conversation_id=topo.active_conversation_id,
        branches=branches,
    )


async def get_tree_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    inc_roles = request.query_params.get("include_role_branches", "true").lower() == "true"
    if rt.protocol_store is None:
        return json_model(
            TreeView(
                root_entry_id=rt.context_tree.root_entry_id,
                active_conversation_id=rt.context_tree.active_conversation_id,
                branches=[],
            )
        )
    topo = await rt.protocol_store.read_tree_topology(include_role_branches=inc_roles)
    return json_model(build_tree_view(topo, rt))


async def create_conversation_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    body = CreateConversationRequest.model_validate_json(await request.body())
    conv = await rt.context_tree.create_root_branch(
        target_title=body.target_title, work_path=body.work_path
    )
    rec = await rt.store.read_record(conv.conversation_id)
    branch = BranchView(
        conversation_id=conv.conversation_id,
        scope="main",
        target_title=body.target_title,
        work_path=await rt.store.get_work_path(conv.conversation_id),
        branch_mode="root_only",
        head_entry_id=rec.head_id if rec else None,
        usage_summary=to_run_usage_view(conv.usage),
        updated_at=utc_rfc3339(),
    )
    if rt.event_bus is not None:
        _ = rt.event_bus.publish(
            "tree.branch_created",
            TreeBranchCreatedPayload(branch=branch, reason="user_create"),
            conversation_id=conv.conversation_id,
        )
    return json_model(branch, status_code=201)


async def switch_conversation_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    conv = await rt.router.switch_conversation(cid)
    return JSONResponse({"conversation_id": conv.conversation_id, "active": True})


async def fork_conversation_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    body = ForkConversationRequest.model_validate_json(await request.body())
    if body.mode == "handoff":
        forked = await rt.context_tree.fork_handoff(
            cid,
            summary=body.summary or body.target_title or "Handoff",
            work_path=body.work_path,
            target_title=body.target_title,
        )
        b_mode: BranchModeLiteral = "handoff"
    else:
        at_idx: int | None = None
        if body.at_entry_id is not None:
            parent_rec = await rt.store.read_record(cid)
            if parent_rec is not None and parent_rec.head_id is not None:
                hist = await rt.store.read_history(parent_rec.head_id, media_window=0)
                for idx, entry in enumerate(hist, start=1):
                    if entry.id == body.at_entry_id:
                        at_idx = idx
                        break
        forked = await rt.store.fork(
            cid, at=at_idx, scope="main", source_scope="main"
        )
        if body.target_title:
            await rt.context_tree.update_target_title(forked.conversation_id, body.target_title)
        if body.work_path:
            _ = await rt.context_tree.bind_workspace(forked.conversation_id, body.work_path)
        if body.switch_to:
            _ = await rt.context_tree.switch_conversation(forked.conversation_id)
        b_mode = "full_fork"

    rec = await rt.store.read_record(forked.conversation_id)
    branch = BranchView(
        conversation_id=forked.conversation_id,
        scope="main",
        target_title=body.target_title,
        work_path=await rt.store.get_work_path(forked.conversation_id),
        branch_mode=b_mode,
        forked_from=cid,
        forked_at=rec.forked_at if rec else None,
        head_entry_id=rec.head_id if rec else None,
        updated_at=utc_rfc3339(),
    )
    if rt.event_bus is not None:
        _ = rt.event_bus.publish(
            "tree.branch_created",
            TreeBranchCreatedPayload(branch=branch, reason="user_fork"),
            conversation_id=forked.conversation_id,
        )
        if body.switch_to:
            _ = rt.event_bus.publish(
                "tree.branch_switched",
                TreeBranchSwitchedPayload(
                    previous_conversation_id=cid,
                    active_conversation_id=forked.conversation_id,
                    reason="user_fork",
                ),
                conversation_id=forked.conversation_id,
            )
    return json_model(branch, status_code=201)


async def patch_conversation_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    body = PatchConversationRequest.model_validate_json(await request.body())
    if body.target_title is not None:
        await rt.context_tree.update_target_title(cid, body.target_title)
    if body.work_path is not None:
        _ = await rt.context_tree.bind_workspace(cid, body.work_path)
    rec = await rt.store.read_record(cid)
    if rec is None:
        return json_error(
            request, status_code=404, code="NOT_FOUND", message=f"Conversation {cid!r} not found."
        )
    branch = BranchView(
        conversation_id=cid,
        scope=rec.scope or "main",
        target_title=body.target_title or "",
        work_path=await rt.store.get_work_path(cid),
        head_entry_id=rec.head_id,
        updated_at=utc_rfc3339(),
    )
    if rt.event_bus is not None:
        _ = rt.event_bus.publish(
            "tree.branch_updated",
            TreeBranchUpdatedPayload(branch=branch),
            conversation_id=cid,
        )
    return json_model(branch)


def _build_turn_page(page: EntryPageData) -> TurnPageView:
    """按 run_id 聚合单页条目为结构化 RunTurnView（含配对后的 thinking/tool/text steps）。"""
    turns: list[RunTurnView] = []
    cur: RunTurnView | None = None
    tool_steps: dict[str, tuple[TurnStepView, int | None]] = {}

    def _start_turn(run_id: str | None, created_at: str) -> RunTurnView:
        nonlocal cur
        tool_steps.clear()
        cur = RunTurnView(run_id=run_id, created_at=created_at)
        turns.append(cur)
        return cur

    for entry in page.entries:
        if entry.kind in ("root", "conv_init"):
            continue
        has_user = any(p.role == "user" for p in entry.parts)
        if cur is None or (
            entry.run_id is not None and entry.run_id != cur.run_id
        ) or (entry.run_id is None and has_user):
            cur = _start_turn(entry.run_id, entry.created_at)

        for cid in entry.child_branch_ids:
            if cid not in cur.child_branch_ids:
                cur.child_branch_ids.append(cid)
        for tid in entry.attached_task_ids:
            if tid not in cur.attached_task_ids:
                cur.attached_task_ids.append(tid)

        for part in entry.parts:
            if part.role == "user":
                if cur.user_entry_id is None:
                    cur.user_entry_id = entry.entry_id
                if part.text:
                    cur.user_text = f"{cur.user_text}\n{part.text}".strip() if cur.user_text else part.text
            elif part.part_kind == "thinking" and part.text:
                cur.steps.append(TurnStepView(kind="thinking", text=part.text))
            elif part.part_kind == "text" and part.text:
                cur.steps.append(TurnStepView(kind="text", text=part.text))
            elif part.part_kind == "tool-call":
                tc_id = part.tool_call_id or ""
                step = TurnStepView(
                    kind="tool",
                    tool_call_id=tc_id or None,
                    tool_name=part.tool_name,
                    status="running",
                    args_text=part.text or "",
                )
                cur.steps.append(step)
                if tc_id:
                    tool_steps[tc_id] = (step, part.timestamp_ms)
            elif part.role == "tool":
                tc_id = part.tool_call_id or ""
                matched = tool_steps.get(tc_id)
                if matched is not None:
                    step, start_ms = matched
                    step.status = "error" if part.is_error else "done"
                    step.result_text = part.text or ""
                    if start_ms is not None and part.timestamp_ms is not None:
                        step.duration_ms = max(0, part.timestamp_ms - start_ms)
                else:
                    cur.steps.append(
                        TurnStepView(
                            kind="tool",
                            tool_call_id=tc_id or None,
                            tool_name=part.tool_name,
                            status="error" if part.is_error else "done",
                            result_text=part.text or "",
                        )
                    )

    return TurnPageView(
        conversation_id=page.conversation_id,
        turns=turns,
        has_more=page.has_more,
        next_before_entry_id=page.next_before_entry_id,
    )


async def get_entries_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    if rt.protocol_store is None:
        return json_error(
            request, status_code=503, code="PROTOCOL_STORE_DISABLED", message="ProtocolStore disabled."
        )
    limit = int(request.query_params.get("limit", "50"))
    before = request.query_params.get("before_entry_id")
    view_mode = request.query_params.get("view", "entries")
    page = await rt.protocol_store.read_entry_page(cid, limit=limit, before_entry_id=before)
    if view_mode == "turns":
        return json_model(_build_turn_page(page))
    view = EntryPageView(
        conversation_id=page.conversation_id,
        has_more=page.has_more,
        next_before_entry_id=page.next_before_entry_id,
        entries=[
            EntryView(
                entry_id=e.entry_id,
                parent_id=e.parent_id,
                kind=e.kind,
                stop_backtrack=e.stop_backtrack,
                run_id=e.run_id,
                child_branch_ids=e.child_branch_ids,
                attached_task_ids=e.attached_task_ids,
                created_at=e.created_at,
                parts=[
                    EntryPartView(
                        part_kind=p.part_kind,
                        role=p.role,
                        text=p.text,
                        tool_name=p.tool_name,
                        tool_call_id=p.tool_call_id,
                        attachment_id=p.attachment_id,
                        content_url=p.content_url,
                    )
                    for p in e.parts
                ],
            )
            for e in page.entries
        ],
    )
    return json_model(view)


def get_tree_routes() -> list[Route]:
    return [
        Route("/v1/tree", get_tree_endpoint, methods=["GET"]),
        Route("/v1/conversations", create_conversation_endpoint, methods=["POST"]),
        Route("/v1/conversations/{cid}/switch", switch_conversation_endpoint, methods=["POST"]),
        Route("/v1/conversations/{cid}/fork", fork_conversation_endpoint, methods=["POST"]),
        Route("/v1/conversations/{cid}", patch_conversation_endpoint, methods=["PATCH"]),
        Route("/v1/conversations/{cid}/entries", get_entries_endpoint, methods=["GET"]),
    ]
