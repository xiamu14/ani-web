"""Router 请求级 Hook: 注入待投递后台事件，并在请求末尾动态注入当前激活工作区规则。"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol, override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.models import ModelRequestContext

from agent.extensions.mcp_config import McpServerSpec, merge_workspace_mcp_specs
from agent.extensions.skill_resolver import SkillCatalogResolver, SkillScope
from agent.permissions import McpCompileReport, McpToolPolicy
from agent.tools import RouterDeps


class _McpSnapshotLike(Protocol):
    @property
    def specs(self) -> tuple[McpServerSpec, ...]: ...
    @property
    def policies(self) -> Mapping[str, McpToolPolicy]: ...
    @property
    def blocked_servers(self) -> frozenset[str]: ...
    @property
    def report(self) -> McpCompileReport: ...


class WorkspaceMcpPort(Protocol):
    async def ensure_compiled(self, work_path: str | Path | None = None) -> _McpSnapshotLike: ...


async def build_workspace_capabilities_tail(
    work_path: str | None,
    *,
    mcp_manager: WorkspaceMcpPort | None = None,
) -> str | None:
    """生成轻量 L1 工作区能力目录（仅 MCP/Skill 名称与摘要，不含 schema/正文）。"""
    mcp_policies: Mapping[str, McpToolPolicy] = {}
    blocked: frozenset[str] = frozenset()
    status_by_name: dict[str, str] = {}
    if mcp_manager is not None:
        snap = await mcp_manager.ensure_compiled(work_path)
        mcp_specs: Sequence[McpServerSpec] = snap.specs
        mcp_policies = snap.policies
        blocked = snap.blocked_servers
        status_by_name = {e.server_name: e.status for e in snap.report.entries}
    else:
        mcp_specs, _ = merge_workspace_mcp_specs((), (), work_path)

    rec_by_server: dict[str, set[str]] = {}
    for pol in mcp_policies.values():
        rec_by_server.setdefault(pol.server_name, set()).update(pol.recommended_roles)

    skill_error: str | None = None
    try:
        catalog = await SkillCatalogResolver().resolve(work_path=work_path)
        skill_entries = catalog.l1_index()
    except (OSError, ValueError) as exc:
        skill_entries = ()
        skill_error = str(exc)

    if not mcp_specs and not skill_entries and skill_error is None:
        return None

    lines: list[str] = [f'<workspace_capabilities work_path="{work_path or ""}">']
    if mcp_specs:
        lines.append("  <mcps>")
        for spec in mcp_specs:
            effective_roles = (
                spec.roles
                if spec.roles is not None
                else tuple(sorted(rec_by_server.get(spec.name, ())))
            )
            st = "blocked" if spec.name in blocked else status_by_name.get(spec.name, "error")
            roles_attr = ",".join(effective_roles) if effective_roles and st in ("compiled", "cached") else "none"
            desc = (spec.description or "").replace('"', "'").strip()[:80]
            desc_attr = f' description="{desc}"' if desc else ""
            lines.append(
                f'    <mcp name="{spec.name}" transport="{spec.transport}" status="{st}" roles="{roles_attr}"{desc_attr} />'
            )
        lines.append("  </mcps>")
    if skill_error is not None:
        lines.append(f'  <skills_error message="{skill_error.replace(chr(34), chr(39))[:120]}" />')
    if skill_entries:
        lines.append("  <skills>")
        for sk in skill_entries:
            scope_str = (
                "workspace"
                if sk.scope == SkillScope.WORKSPACE
                else ("global" if sk.scope == SkillScope.GLOBAL else "builtin")
            )
            desc = sk.description.replace('"', "'").strip()[:80]
            lines.append(
                f'    <skill name="{sk.name}" scope="{scope_str}" description="{desc}" />'
            )
        lines.append("  </skills>")
    lines.append("</workspace_capabilities>")
    return "\n".join(lines)


class WorkspacePromptPort(Protocol):
    """读取当前激活工作区规则的最小端口协议。"""

    async def get_active_workspace_prompt(self, conversation_id: str) -> str | None: ...


class RouterContextHook(AbstractCapability[RouterDeps]):
    """在每次模型请求前处理事件入队与尾部 AGENTS.md 动态注入（不污染持久化历史）。"""

    def __init__(
        self,
        pending_events: dict[str, deque[str]],
        workspace_prompt: WorkspacePromptPort,
        *,
        mcp_manager: WorkspaceMcpPort | None = None,
    ) -> None:
        self._pending_events: dict[str, deque[str]] = pending_events
        self._workspace_prompt: WorkspacePromptPort = workspace_prompt
        self._mcp_manager: WorkspaceMcpPort | None = mcp_manager

    async def before_model_request(  # pyright: ignore[reportImplicitOverride]
        self, ctx: RunContext[RouterDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        queue = self._pending_events.get(ctx.deps.main_conversation_id)
        if queue:
            while queue:
                _ = ctx.enqueue(queue.popleft(), priority="asap")

        ws_prompt = await self._workspace_prompt.get_active_workspace_prompt(
            ctx.deps.main_conversation_id
        )
        work_path = await ctx.deps.workspace.get_work_path(ctx.deps.main_conversation_id)
        caps_tail = await build_workspace_capabilities_tail(
            work_path, mcp_manager=self._mcp_manager
        )
        tail_parts = [p for p in (ws_prompt, caps_tail) if p]
        if tail_parts and request_context.messages:
            ws_msg = ModelRequest(
                parts=[SystemPromptPart(content="\n\n".join(tail_parts))]
            )
            return replace(
                request_context,
                messages=[*request_context.messages[:-1], ws_msg, request_context.messages[-1]],
            )
        return request_context


class RouterSteerHook(AbstractCapability[RouterDeps]):
    """在 Router 运行中每次模型请求前实时注入用户追加建议（Interjection）。"""

    def __init__(
        self,
        steer_queues: dict[str, deque[str]],
        *,
        max_injections: int = 10,
    ) -> None:
        self._steer_queues: dict[str, deque[str]] = steer_queues
        self._max_injections: int = max(0, max_injections)
        self._injected_counts: dict[str, int] = {}

    @override
    async def before_model_request(
        self, ctx: RunContext[RouterDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        cid = ctx.deps.main_conversation_id
        queue = self._steer_queues.get(cid)
        if not queue:
            return request_context
        count = self._injected_counts.get(cid, 0)
        while queue and count < self._max_injections:
            instruction = queue.popleft()
            count += 1
            _ = ctx.enqueue(
                f"<user_interjection>\n{instruction}\n</user_interjection>",
                priority="asap",
            )
        self._injected_counts[cid] = count
        return request_context
