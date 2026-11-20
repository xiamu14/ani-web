"""权限拦截 Hook: 在工具实际执行前以最高优先级异步阻塞校验权限。"""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any, override

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from agent.core.budget import LoopSafetyAbortError
from agent.core.outcome_judge import ToolExecutionTrace
from agent.hooks.priority import HookPriority
from agent.permissions import PermissionReviewer


def canonicalize_tool_args(args: dict[str, Any]) -> str:  # pyright: ignore[reportExplicitAny]
    """将工具参数字典序列化为确定性 JSON 指纹，用于重复调用与重复拒权检测。"""
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        return repr(sorted(args.items()))


class PermissionGuardHook(AbstractCapability[object]):
    """最高优先级 before_tool_execute 权限拦截器（首次拒绝抛 ModelRetry，二次重复调用直接抛 LoopSafetyAbortError 短路）。"""

    hook_priority: int = int(HookPriority.PERMISSION)

    def __init__(
        self,
        *,
        reviewer: PermissionReviewer,
        workspace_root: str | Path | None = None,
        user_intent: str = "",
        known_mcp_prefixes: Collection[str] = (),
        trace: ToolExecutionTrace | None = None,
    ) -> None:
        self._reviewer: PermissionReviewer = reviewer
        self._workspace_root: str | Path | None = workspace_root
        self._user_intent: str = user_intent
        self._known_mcp_prefixes: tuple[str, ...] = tuple(
            p if p.endswith("_") else f"{p}_" for p in known_mcp_prefixes if p
        )
        self._trace: ToolExecutionTrace | None = trace
        self._seen_run_ids: set[str] = set()
        self._denied_signatures: dict[tuple[str, str], str] = {}

    def release_runs(self) -> None:
        """在 Worker 退出（含异常/取消）时清理本次运行产生的所有临时权限授权。"""
        for run_id in list(self._seen_run_ids):
            self._reviewer.clear_run(run_id)
        self._seen_run_ids.clear()
        self._denied_signatures.clear()

    @override
    async def before_tool_execute(
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        run_id = ctx.run_id or "default-run"
        self._seen_run_ids.add(run_id)
        sig = (call.tool_name, canonicalize_tool_args(args))
        prior_denial = self._denied_signatures.get(sig)
        if prior_denial is not None:
            if self._trace is not None:
                self._trace.permission_denials += 1
            raise LoopSafetyAbortError(
                f"Permission repeatedly denied for {call.tool_name}: {prior_denial}",
                kind="permission_denied",
                tool_name=call.tool_name,
                streak=2,
            )

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
            reason_msg = f"[{res.decision_source}]: {res.reason}"
            self._denied_signatures[sig] = reason_msg
            if self._trace is not None:
                self._trace.permission_denials += 1
            raise ModelRetry(
                f"权限拒绝 {reason_msg}。严禁使用相同参数再次调用 `{call.tool_name}`；"
                + "请跳过该受限操作或改用允许的工具继续。"
            )
        return args
