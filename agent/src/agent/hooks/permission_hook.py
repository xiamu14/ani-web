"""权限拦截 Hook: 在工具实际执行前以最高优先级异步阻塞校验权限。"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any, override

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from agent.hooks.priority import HookPriority
from agent.permissions import PermissionReviewer


class PermissionGuardHook(AbstractCapability[object]):
    """最高优先级 before_tool_execute 权限拦截器（拒绝时抛出 ModelRetry 短路）。"""

    hook_priority: int = int(HookPriority.PERMISSION)

    def __init__(
        self,
        *,
        reviewer: PermissionReviewer,
        workspace_root: str | Path | None = None,
        user_intent: str = "",
        known_mcp_prefixes: Collection[str] = (),
    ) -> None:
        self._reviewer: PermissionReviewer = reviewer
        self._workspace_root: str | Path | None = workspace_root
        self._user_intent: str = user_intent
        self._known_mcp_prefixes: tuple[str, ...] = tuple(
            p if p.endswith("_") else f"{p}_" for p in known_mcp_prefixes if p
        )
        self._seen_run_ids: set[str] = set()

    def release_runs(self) -> None:
        """在 Worker 退出（含异常/取消）时清理本次运行产生的所有临时权限授权。"""
        for run_id in list(self._seen_run_ids):
            self._reviewer.clear_run(run_id)
        self._seen_run_ids.clear()

    @override
    async def before_tool_execute(
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        _ = tool_def
        run_id = ctx.run_id or "default-run"
        self._seen_run_ids.add(run_id)
        intent = self._user_intent or (ctx.prompt if isinstance(ctx.prompt, str) else "")
        meta = tool_def.metadata if isinstance(tool_def.metadata, dict) else {}
        is_mcp_tool = bool(meta.get("is_mcp_tool")) or any(
            call.tool_name.startswith(prefix) for prefix in self._known_mcp_prefixes
        )
        raw_hash = meta.get("mcp_schema_hash")
        runtime_schema_hash = raw_hash if isinstance(raw_hash, str) else None
        res = await self._reviewer.authorize(
            run_id=run_id,
            tool_name=call.tool_name,
            args=args,
            workspace_root=self._workspace_root,
            user_intent=intent,
            is_mcp_tool=is_mcp_tool,
            runtime_schema_hash=runtime_schema_hash,
        )
        if not res.allowed:
            raise ModelRetry(f"权限拒绝 [{res.decision_source}]: {res.reason}")
        return args
