"""Router 专属工具集与解耦端口协议（面向 WorkspacePort / TaskDispatchPort 编程）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import FunctionToolset

LockMode = Literal["exclusive", "shared", "none"]


class WorkspaceBindingLike(Protocol):
    """工作区绑定结果的最小结构协议。"""

    @property
    def work_path(self) -> str: ...

    @property
    def agents_md(self) -> str | None: ...


class WorkspacePort(Protocol):
    """Context Tree 工作区绑定端口（隔离具体存储实现）。"""

    async def bind_workspace(self, conversation_id: str, path: str) -> WorkspaceBindingLike: ...

    async def get_work_path(self, conversation_id: str) -> str | None: ...


@dataclass(frozen=True, kw_only=True)
class TaskSummary:
    """暴露给 Router 的任务摘要视图。"""

    task_id: str
    role_name: str
    status: str
    work_path: str | None
    mandate: str


@dataclass(frozen=True, kw_only=True)
class DispatchReceipt:
    """后台任务提交回执。"""

    task_id: str
    status: str
    queue_position: int = 0
    work_path: str | None = None


@dataclass(frozen=True, kw_only=True)
class WorkspaceLockStatus:
    """工作区当前读写锁状态快照。"""

    work_path: str
    exclusive_holder: str | None = None
    shared_holders: tuple[str, ...] = ()
    queued_tasks: tuple[str, ...] = ()


class TaskDispatchPort(Protocol):
    """后台 Role Worker 调度端口（隔离 WorkspaceLockManager 具体实现）。"""

    async def dispatch_task(
        self,
        *,
        main_conversation_id: str,
        role_name: str,
        mandate: str,
        work_path: str | None,
        lock_mode: LockMode | None = None,
        depends_on: Sequence[str] | None = None,
    ) -> DispatchReceipt: ...

    async def steer_or_revise(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        instruction: str,
    ) -> str: ...

    async def cancel_task(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
    ) -> str: ...

    async def list_tasks(self, main_conversation_id: str) -> Sequence[TaskSummary]: ...

    async def inspect_workspace_locks(
        self, work_path: str | None = None
    ) -> Sequence[WorkspaceLockStatus]: ...


@dataclass(kw_only=True)
class RouterDeps:
    """Router 单次运行注入的依赖上下文。"""

    main_conversation_id: str
    workspace: WorkspacePort
    tasks: TaskDispatchPort | None = None


def _require_tasks(deps: RouterDeps) -> TaskDispatchPort:
    if deps.tasks is None:
        raise ModelRetry("后台任务调度器尚未启用，当前仅支持直接对话与工作区绑定。")
    return deps.tasks


def _format_task_line(task: TaskSummary) -> str:
    ws = task.work_path or "(无工作区)"
    preview = task.mandate if len(task.mandate) <= 60 else f"{task.mandate[:60]}..."
    return f"- @{task.task_id} [{task.role_name}] ({task.status}, {ws}): {preview}"


async def _resolve_workspace_arg(
    ctx: RunContext[RouterDeps],
    raw_path: str,
) -> str:
    """将相对路径优先相对当前会话已绑定工作区解析为规范化路径。"""
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve())
    base_ws = await ctx.deps.workspace.get_work_path(ctx.deps.main_conversation_id)
    if base_ws is not None:
        resolved = (Path(base_ws) / candidate).resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ModelRetry(f"工作区子目录不存在或不是目录: {resolved}")
        return str(resolved)
    return str(candidate.resolve())


async def set_workspace(ctx: RunContext[RouterDeps], path: str) -> str:
    """绑定或切换当前主对话的工作区目录，并加载该目录自身的 AGENTS.md。

    Args:
        path: 目标工作区目录路径（支持相对路径、相对当前工作区的子目录或 ~ 展开）。
    """
    cleaned = path.strip()
    if not cleaned:
        raise ModelRetry("工作区路径不能为空。")
    resolved_target = await _resolve_workspace_arg(ctx, cleaned)
    try:
        binding = await ctx.deps.workspace.bind_workspace(ctx.deps.main_conversation_id, resolved_target)
    except (ValueError, LookupError) as exc:
        raise ModelRetry(str(exc)) from exc

    rules_hint = "已加载该目录 AGENTS.md。" if binding.agents_md is not None else "该目录未配置 AGENTS.md。"
    return f"工作区已绑定为 {binding.work_path}。{rules_hint}"


async def dispatch_role_agent(
    ctx: RunContext[RouterDeps],
    role_name: str,
    mandate: str,
    work_path: str | None = None,
    lock_mode: LockMode | None = None,
    depends_on: list[str] | None = None,
) -> str:
    """向指定 Role Agent 异步派发后台任务，立即返回 @task_id 回执而不阻塞对话。

    Args:
        role_name: 目标角色名（如 coder、reviewer、tester、researcher）。
        mandate: 清晰完整的任务指令。
        work_path: 可选工作目录；省略时自动沿用当前会话绑定的工作区（除非显式指定 lock_mode='none'）。
        lock_mode: 可选锁模式（'exclusive' 写互斥、'shared' 只读共享、'none' 无目录锁）；省略时按角色默认属性。
        depends_on: 可选前置依赖任务 ID 列表（如 ['T1']），需等对应任务完成后再启动。
    """
    scheduler = _require_tasks(ctx.deps)
    role = role_name.strip().lstrip("@")
    task_mandate = mandate.strip()
    if not role:
        raise ModelRetry("role_name 不能为空。")
    if not task_mandate:
        raise ModelRetry("mandate 不能为空。")

    raw_ws = work_path.strip() if work_path is not None and work_path.strip() else None
    if raw_ws is not None:
        target_ws: str | None = await _resolve_workspace_arg(ctx, raw_ws)
    elif lock_mode != "none":
        target_ws = await ctx.deps.workspace.get_work_path(ctx.deps.main_conversation_id)
    else:
        target_ws = None

    try:
        receipt = await scheduler.dispatch_task(
            main_conversation_id=ctx.deps.main_conversation_id,
            role_name=role,
            mandate=task_mandate,
            work_path=target_ws,
            lock_mode=lock_mode,
            depends_on=depends_on,
        )
    except (LookupError, ValueError) as exc:
        raise ModelRetry(str(exc)) from exc

    ws_label = receipt.work_path or "(无工作区)"
    if receipt.status == "queued":
        return (
            f"任务 @{receipt.task_id} (@{role}, {ws_label}) 已进入排队队列第 {receipt.queue_position} 位，"
            "前序任务完成后将自动启动。你可以继续提问或派发其他任务。"
        )
    return (
        f"任务 @{receipt.task_id} (@{role}, {ws_label}) 已在后台启动。"
        "你可以继续提问，或通过 @task_id 对其进行实时纠偏。"
    )


async def steer_or_revise_task(
    ctx: RunContext[RouterDeps],
    task_id: str,
    instruction: str,
    user_confirmed: bool = False,
) -> str:
    """对指定 @task_id 执行运行中纠偏（Steer）、排队追加或完成后返工（Revise）。

    Args:
        task_id: 目标任务 ID（如 T1）。
        instruction: 新的纠偏或返工指令。
        user_confirmed: 仅当用户显式引用 @task_id 或已明确确认目标时传 True；否则传 False 触发确认。
    """
    scheduler = _require_tasks(ctx.deps)
    norm_id = task_id.strip().lstrip("@")
    cleaned_instruction = instruction.strip()
    if not norm_id:
        raise ModelRetry("task_id 不能为空。")
    if not cleaned_instruction:
        raise ModelRetry("instruction 不能为空。")

    if not user_confirmed:
        tasks = await scheduler.list_tasks(ctx.deps.main_conversation_id)
        matched = next((t for t in tasks if t.task_id == norm_id), None)
        if matched is None:
            listing = "\n".join(_format_task_line(t) for t in tasks) or "(当前暂无任务)"
            raise ModelRetry(f"未找到任务 @{norm_id}。当前任务列表:\n{listing}")
        ws = matched.work_path or "(无工作区)"
        return (
            f"CONFIRM_NEEDED: 用户未显式引用 @{norm_id}，请先向用户确认是否针对 "
            f"@{norm_id} (@{matched.role_name}, {matched.status}, {ws}) 执行：{cleaned_instruction}"
        )

    try:
        return await scheduler.steer_or_revise(
            main_conversation_id=ctx.deps.main_conversation_id,
            task_id=norm_id,
            instruction=cleaned_instruction,
        )
    except (LookupError, ValueError) as exc:
        raise ModelRetry(str(exc)) from exc


async def cancel_task(
    ctx: RunContext[RouterDeps],
    task_id: str,
    user_confirmed: bool = False,
) -> str:
    """取消指定 @task_id（运行中、排队中、失败或中断的任务），并自动解除下游任务对其的依赖。

    Args:
        task_id: 目标任务 ID（如 T1）。
        user_confirmed: 仅当用户显式引用 @task_id 或已确认取消目标时传 True；否则传 False 触发确认。
    """
    scheduler = _require_tasks(ctx.deps)
    norm_id = task_id.strip().lstrip("@")
    if not norm_id:
        raise ModelRetry("task_id 不能为空。")

    if not user_confirmed:
        tasks = await scheduler.list_tasks(ctx.deps.main_conversation_id)
        matched = next((t for t in tasks if t.task_id == norm_id), None)
        if matched is None:
            listing = "\n".join(_format_task_line(t) for t in tasks) or "(当前暂无任务)"
            raise ModelRetry(f"未找到任务 @{norm_id}。当前任务列表:\n{listing}")
        ws = matched.work_path or "(无工作区)"
        return (
            f"CONFIRM_NEEDED: 用户未显式引用 @{norm_id}，请先向用户确认是否取消 "
            f"@{norm_id} (@{matched.role_name}, {matched.status}, {ws})？取消后将自动解除下游对其依赖。"
        )

    try:
        return await scheduler.cancel_task(
            main_conversation_id=ctx.deps.main_conversation_id,
            task_id=norm_id,
        )
    except (LookupError, ValueError) as exc:
        raise ModelRetry(str(exc)) from exc


async def list_tasks(ctx: RunContext[RouterDeps]) -> str:
    """列出当前主对话下所有后台任务（@task_id、角色、状态、目录与指令摘要）。"""
    scheduler = _require_tasks(ctx.deps)
    tasks = await scheduler.list_tasks(ctx.deps.main_conversation_id)
    if not tasks:
        return "当前主对话暂无后台任务。"
    return "\n".join(_format_task_line(t) for t in tasks)


async def inspect_workspace_locks(
    ctx: RunContext[RouterDeps],
    work_path: str | None = None,
) -> str:
    """查询工作区当前的读写锁占用与排队情况，辅助规划多任务并发或依赖顺序。

    Args:
        work_path: 可选目录路径；省略时列出全部活跃工作区锁状态。
    """
    scheduler = _require_tasks(ctx.deps)
    raw_target = work_path.strip() if work_path is not None and work_path.strip() else None
    target = await _resolve_workspace_arg(ctx, raw_target) if raw_target is not None else None
    snapshots = await scheduler.inspect_workspace_locks(target)
    if not snapshots:
        return "当前无活跃的工作区读写锁或等待队列。"
    lines: list[str] = []
    for snap in snapshots:
        parts = [f"工作区 {snap.work_path}:"]
        if snap.exclusive_holder:
            parts.append(f"写锁(exclusive)={snap.exclusive_holder}")
        if snap.shared_holders:
            parts.append(f"读锁(shared)={','.join(snap.shared_holders)}")
        if snap.queued_tasks:
            parts.append(f"排队={','.join(snap.queued_tasks)}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def build_router_toolset() -> FunctionToolset[RouterDeps]:
    """构建 Router 专属工具集（绑定 RouterDeps 依赖类型）。"""
    toolset = FunctionToolset[RouterDeps](id="router_tools")
    _ = toolset.add_function(set_workspace, takes_ctx=True)
    _ = toolset.add_function(dispatch_role_agent, takes_ctx=True)
    _ = toolset.add_function(steer_or_revise_task, takes_ctx=True)
    _ = toolset.add_function(cancel_task, takes_ctx=True)
    _ = toolset.add_function(list_tasks, takes_ctx=True)
    _ = toolset.add_function(inspect_workspace_locks, takes_ctx=True)
    return toolset
