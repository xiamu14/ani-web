"""Layer 2 Context Window 9 桶快照与三层 Token 用量遥测路由。"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent.core.budget import ROUTER_BUDGET_POLICY
from agent.extensions.mcp_config import load_mcp_configs
from agent.extensions.skill_resolver import SkillCatalogResolver, SkillScope
from agent.protocol import (
    ContextWindowSnapshotView,
    McpServerView,
    McpsListView,
    McpToolPolicyView,
    RoleTaskUsageItemView,
    RunUsageView,
    SkillScopeLiteral,
    SkillsListView,
    SkillView,
    SkippedSkillView,
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


def _scope_literal(scope: SkillScope) -> SkillScopeLiteral:
    if scope == SkillScope.WORKSPACE:
        return "workspace"
    if scope == SkillScope.GLOBAL:
        return "global"
    return "builtin"


async def get_skills_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    work_path = request.query_params.get("work_path")
    if work_path is None:
        cid = request.query_params.get("conversation_id") or rt.context_tree.active_conversation_id
        if cid:
            work_path = await rt.store.get_work_path(cid)
    catalog = await SkillCatalogResolver().resolve(work_path=work_path)
    return json_model(
        SkillsListView(
            work_path=str(catalog.work_path) if catalog.work_path is not None else None,
            skills=[
                SkillView(
                    name=s.name,
                    description=s.description,
                    when_to_use=s.when_to_use,
                    scope=_scope_literal(s.scope),
                    version=s.version_raw,
                    directory=s.directory,
                    path=s.path,
                    model_invocable=s.model_invocable,
                )
                for s in catalog.skills
            ],
            skipped=[
                SkippedSkillView(
                    path=str(sk.path),
                    reason=str(sk.reason),
                    message=str(sk.message),
                )
                for sk in catalog.skipped
            ],
        )
    )


async def get_mcps_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    work_path = request.query_params.get("work_path")
    if work_path is None:
        cid = request.query_params.get("conversation_id") or rt.context_tree.active_conversation_id
        if cid:
            work_path = await rt.store.get_work_path(cid)

    # 优先通过 WorkspaceMcpManager 获取该工作区的已编译快照（含 OAuth 检查与 Jev 定级）
    if rt.mcp_manager is not None:
        snap = await rt.mcp_manager.ensure_compiled(work_path)
        merged_specs = list(snap.specs)
        compile_by_name = {e.server_name: e for e in snap.report.entries}
        blocked_set = snap.blocked_servers
        policies_source = snap.policies
        total_jev_calls = rt.mcp_manager.total_jev_calls
    else:
        merged_specs = list(rt.mcp_specs)
        seen_names = {s.name for s in merged_specs}
        ws_root = Path(work_path).expanduser().resolve() if work_path else None
        try:
            loaded = load_mcp_configs(workspace_root=ws_root)
            for spec in loaded.servers:
                if spec.name not in seen_names:
                    merged_specs.append(spec)
                    seen_names.add(spec.name)
        except (OSError, ValueError):
            pass
        compile_by_name = {
            e.server_name: e
            for e in (rt.mcp_compile_report.entries if rt.mcp_compile_report is not None else ())
        }
        blocked_set = rt.mcp_blocked_servers
        policies_source = rt.mcp_policies
        total_jev_calls = (
            rt.mcp_compile_report.total_jev_calls
            if rt.mcp_compile_report is not None
            else 0
        )

    policies_by_server: dict[str, list[McpToolPolicyView]] = {}
    for pol in policies_source.values():
        policies_by_server.setdefault(pol.server_name, []).append(
            McpToolPolicyView(
                tool_name=pol.tool_name,
                prefixed_tool_name=pol.prefixed_tool_name,
                level=str(pol.level),
                lock_mode=str(pol.lock_mode),
                reason=pol.reason,
            )
        )
    servers: list[McpServerView] = []
    for spec in merged_specs:
        comp = compile_by_name.get(spec.name)
        servers.append(
            McpServerView(
                name=spec.name,
                scope=spec.scope,
                transport=spec.transport,
                description=spec.description,
                command=spec.command,
                url=spec.url,
                roles=list(spec.roles) if spec.roles is not None else None,
                defer_loading=spec.defer_loading,
                blocked=spec.name in blocked_set,
                compile_status=comp.status if comp is not None else None,
                compile_error=comp.error if comp is not None and comp.error else None,
                tools=policies_by_server.get(spec.name, []),
            )
        )
    return json_model(
        McpsListView(servers=servers, total_jev_calls=total_jev_calls)
    )


def get_telemetry_routes() -> list[Route]:
    return [
        Route("/v1/conversations/{cid}/context", get_context_endpoint, methods=["GET"]),
        Route("/v1/conversations/{cid}/usage", get_usage_endpoint, methods=["GET"]),
        Route("/v1/skills", get_skills_endpoint, methods=["GET"]),
        Route("/v1/mcps", get_mcps_endpoint, methods=["GET"]),
    ]
