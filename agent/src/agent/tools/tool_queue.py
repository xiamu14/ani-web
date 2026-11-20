"""Agent Loop 内 Tools 任务队列: 按状态与副作用执行异步并发与串行读写锁调度。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from agent.db import paths_overlap
from agent.hooks.priority import HookPriority
from agent.tools.router_tools import LockMode

ResourceKind = Literal["path", "exact", "none"]

_PATH_SHARED_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "file_info",
        "list_directory",
        "list_files",
        "search_files",
        "find_files",
        "grep",
    }
)
_PATH_EXCLUSIVE_TOOLS: frozenset[str] = frozenset(
    {"write_file", "edit_file", "create_directory"}
)
_SHELL_EXCLUSIVE_TOOLS: frozenset[str] = frozenset({"shell", "run_command", "start_command"})
_CMD_HANDLE_TOOLS: frozenset[str] = frozenset({"check_command", "stop_command"})
_NO_LOCK_TOOLS: frozenset[str] = frozenset(
    {
        "list_tasks",
        "inspect_workspace_locks",
        "load_capability",
        "search_tools",
        "memory_search",
        "memory_timeline",
        "get_observations",
    }
)
_MEMORY_WRITE_TOOLS: frozenset[str] = frozenset({"save_memory"})
_NO_LOCK_TOOL_KINDS: frozenset[str] = frozenset({"capability-load", "tool-search"})
_CONV_EXCLUSIVE_TOOLS: frozenset[str] = frozenset({"set_workspace", "dispatch_role_agent"})
_TICKET_EXCLUSIVE_TOOLS: frozenset[str] = frozenset({"steer_or_revise_task", "cancel_task"})
_PATH_ARG_KEYS: tuple[str, ...] = ("path", "file_path", "target_file", "directory", "dir_path")


@dataclass(frozen=True, kw_only=True)
class ToolResource:
    """单个工具调用对应的资源键与锁模式。"""

    kind: ResourceKind
    key: str
    lock_mode: LockMode


class McpLockInfo(Protocol):
    """MCP 工具队列锁只读视图（解耦权限与存储实现）。"""

    @property
    def endpoint_fingerprint(self) -> str: ...

    @property
    def lock_mode(self) -> LockMode: ...

    @property
    def schema_hash(self) -> str: ...


def _extract_str(args: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _resolve_path_key(raw_path: str | None, workspace_root: Path | None) -> str:
    if raw_path is None:
        return str(workspace_root or Path.cwd().resolve())
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute() or workspace_root is None:
        return str(candidate.resolve())
    return str((workspace_root / candidate).resolve())


def resolve_tool_resource(
    tool_name: str,
    args: Mapping[str, object],
    *,
    workspace_root: str | Path | None = None,
    main_conversation_id: str | None = None,
    tool_kind: str | None = None,
    mcp_policy: McpLockInfo | None = None,
    is_mcp_tool: bool = False,
    runtime_schema_hash: str | None = None,
) -> ToolResource:
    """对照工具清单表解析资源键与读写锁模式。"""
    if tool_kind in _NO_LOCK_TOOL_KINDS or tool_name in _NO_LOCK_TOOLS:
        return ToolResource(kind="none", key="", lock_mode="none")
    if tool_name in _MEMORY_WRITE_TOOLS:
        return ToolResource(kind="exact", key="memory:save", lock_mode="exclusive")

    if mcp_policy is not None:
        drifted = runtime_schema_hash is not None and runtime_schema_hash != mcp_policy.schema_hash
        return ToolResource(
            kind="exact",
            key=f"mcp_server:{mcp_policy.endpoint_fingerprint}",
            lock_mode="exclusive" if drifted else mcp_policy.lock_mode,
        )
    if is_mcp_tool:
        return ToolResource(
            kind="exact",
            key=f"mcp_unregistered:{tool_name}",
            lock_mode="exclusive",
        )

    norm_ws = Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
    conv_id = main_conversation_id or "default"

    if tool_name in _PATH_SHARED_TOOLS:
        key = _resolve_path_key(_extract_str(args, _PATH_ARG_KEYS), norm_ws)
        return ToolResource(kind="path", key=key, lock_mode="shared")
    if tool_name in _PATH_EXCLUSIVE_TOOLS:
        key = _resolve_path_key(_extract_str(args, _PATH_ARG_KEYS), norm_ws)
        return ToolResource(kind="path", key=key, lock_mode="exclusive")
    if tool_name in _SHELL_EXCLUSIVE_TOOLS:
        key = str(norm_ws or Path.cwd().resolve())
        return ToolResource(kind="path", key=key, lock_mode="exclusive")
    if tool_name in _CMD_HANDLE_TOOLS:
        cmd_id = _extract_str(args, ("command_id", "id", "handle")) or "default"
        return ToolResource(kind="exact", key=f"cmd:{cmd_id}", lock_mode="exclusive")
    if tool_name in _CONV_EXCLUSIVE_TOOLS:
        return ToolResource(kind="exact", key=f"conv:{conv_id}", lock_mode="exclusive")
    if tool_name in _TICKET_EXCLUSIVE_TOOLS:
        tid = (_extract_str(args, ("task_id",)) or "default").lstrip("@")
        return ToolResource(kind="exact", key=f"ticket:{conv_id}:{tid}", lock_mode="exclusive")

    if norm_ws is not None:
        return ToolResource(kind="path", key=str(norm_ws), lock_mode="exclusive")
    return ToolResource(kind="exact", key=f"tool:{tool_name}", lock_mode="exclusive")


def _resources_conflict(a: ToolResource, b: ToolResource) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "none":
        return False
    if a.kind == "path":
        return paths_overlap(a.key, b.key)
    return a.key == b.key


class ToolLockCoordinator:
    """进程级/Runtime 共享的全工具读写锁协调器（支持跨 Role、跨 Agent Loop 并发与串行）。"""

    def __init__(self) -> None:
        self._cond: asyncio.Condition = asyncio.Condition()
        self._seq: int = 0
        self._active: dict[int, ToolResource] = {}
        self._waiting: list[tuple[int, ToolResource]] = []

    def _can_acquire(self, waiter_id: int, res: ToolResource) -> bool:
        for active_res in self._active.values():
            if not _resources_conflict(res, active_res):
                continue
            if res.lock_mode == "exclusive" or active_res.lock_mode == "exclusive":
                return False

        for q_id, q_res in self._waiting:
            if q_id == waiter_id:
                break
            if not _resources_conflict(res, q_res):
                continue
            if res.lock_mode == "exclusive" or q_res.lock_mode == "exclusive":
                return False
        return True

    @asynccontextmanager
    async def acquire(self, res: ToolResource) -> AsyncGenerator[None]:
        async with self._cond:
            self._seq += 1
            holder_id = self._seq
            self._waiting.append((holder_id, res))
            try:
                while not self._can_acquire(holder_id, res):
                    _ = await self._cond.wait()
                self._waiting = [(qid, qres) for qid, qres in self._waiting if qid != holder_id]
                self._active[holder_id] = res
            except asyncio.CancelledError:
                self._waiting = [(qid, qres) for qid, qres in self._waiting if qid != holder_id]
                self._cond.notify_all()
                raise

        try:
            yield
        finally:
            async with self._cond:
                _ = self._active.pop(holder_id, None)
                self._cond.notify_all()


class ToolExecutionQueueHook(AbstractCapability[object]):
    """基于 wrap_tool_execute 的 Tools 任务队列（可绑定共享 ToolLockCoordinator）。"""

    hook_priority: int = int(HookPriority.TOOL_QUEUE)

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        coordinator: ToolLockCoordinator | None = None,
        mcp_policies: Mapping[str, McpLockInfo] | None = None,
        known_mcp_prefixes: Collection[str] = (),
    ) -> None:
        self._workspace_root: str | Path | None = workspace_root
        self._coordinator: ToolLockCoordinator = (
            coordinator if coordinator is not None else ToolLockCoordinator()
        )
        self._mcp_policies: dict[str, McpLockInfo] = dict(mcp_policies or {})
        self._known_mcp_prefixes: tuple[str, ...] = tuple(
            p if p.endswith("_") else f"{p}_" for p in known_mcp_prefixes if p
        )

    @override
    async def wrap_tool_execute(  # pyright: ignore[reportAny]
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
        handler: WrapToolExecuteHandler,
    ) -> Any:  # pyright: ignore[reportExplicitAny]
        main_conv_id = getattr(ctx.deps, "main_conversation_id", None)
        meta = tool_def.metadata if isinstance(tool_def.metadata, dict) else {}
        is_mcp_tool = bool(meta.get("is_mcp_tool")) or any(
            call.tool_name.startswith(prefix) for prefix in self._known_mcp_prefixes
        )
        raw_hash = meta.get("mcp_schema_hash")
        runtime_schema_hash = raw_hash if isinstance(raw_hash, str) else None
        res = resolve_tool_resource(
            call.tool_name,
            args,
            workspace_root=self._workspace_root,
            main_conversation_id=main_conv_id if isinstance(main_conv_id, str) else None,
            tool_kind=tool_def.tool_kind,
            mcp_policy=self._mcp_policies.get(call.tool_name),
            is_mcp_tool=is_mcp_tool,
            runtime_schema_hash=runtime_schema_hash,
        )
        if res.lock_mode == "none":
            return await handler(args)  # pyright: ignore[reportAny]
        async with self._coordinator.acquire(res):
            return await handler(args)  # pyright: ignore[reportAny]
