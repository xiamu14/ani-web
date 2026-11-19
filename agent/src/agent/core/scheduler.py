"""工作目录读写锁与异步 Role Worker 调度器（实现 TaskDispatchPort 协议）。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence

import logfire
from opentelemetry import context as otel_context
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.conversation_store import ConversationStore
from pydantic_ai.exceptions import AgentRunError, ModelAPIError, UsageLimitExceeded
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.step_persistence import StepStore

from agent.core.lock_queue import LockQueueState, QueuedTask, as_lock_mode, format_worker_error
from agent.core.roles import RoleRegistry, RoleSessionManager, RoleSpec
from agent.db import SQLModelTicketStore, parse_depends_on
from agent.hooks import PermissionGuardHook, TaskSteerHook
from agent.permissions import PermissionReviewer
from agent.tools import (
    DispatchReceipt,
    LockMode,
    TaskSummary,
    ToolExecutionQueueHook,
    ToolLockCoordinator,
    WorkspaceLockStatus,
)

TicketEventCallback = Callable[[str, str, str], Awaitable[object]]


class WorkspaceLockManager:
    """按规范化 work_path（含父子目录重叠）维护读写锁、Steer/Revise 与恢复调度器。"""

    def __init__(
        self,
        *,
        roles: RoleRegistry,
        sessions: RoleSessionManager,
        ticket_store: SQLModelTicketStore,
        step_store: StepStore | None = None,
        on_event: TicketEventCallback | None = None,
        permission_reviewer: PermissionReviewer | None = None,
        tool_coordinator: ToolLockCoordinator | None = None,
    ) -> None:
        self._roles: RoleRegistry = roles
        self._sessions: RoleSessionManager = sessions
        self._ticket_store: SQLModelTicketStore = ticket_store
        self._step_store: StepStore | None = step_store
        self._on_event: TicketEventCallback | None = on_event
        self._permission_reviewer: PermissionReviewer | None = permission_reviewer
        self._tool_coordinator: ToolLockCoordinator = (
            tool_coordinator if tool_coordinator is not None else ToolLockCoordinator()
        )
        self._state: LockQueueState = LockQueueState()
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._steer_queues: dict[tuple[str, str], deque[str]] = {}
        self._run_prompts: dict[tuple[str, str], str] = {}
        self._dispatch_lock: asyncio.Lock = asyncio.Lock()

    def set_event_handler(self, handler: TicketEventCallback) -> None:
        """在 Router 装配完成后绑定事件回推回调。"""
        self._on_event = handler

    @staticmethod
    def _resolve_lock_mode(
        role: RoleSpec,
        norm_path: str | None,
        requested: LockMode | None,
    ) -> LockMode:
        if norm_path is None:
            return "none"
        if role.default_lock_mode == "exclusive" or requested == "exclusive":
            return "exclusive"
        return "shared"

    async def dispatch_task(
        self,
        *,
        main_conversation_id: str,
        role_name: str,
        mandate: str,
        work_path: str | None,
        lock_mode: LockMode | None = None,
        depends_on: Sequence[str] | None = None,
    ) -> DispatchReceipt:
        """非阻塞提交任务：读写锁空闲且前置依赖满足时立即启动，否则按 FIFO 入队等待。"""
        role = self._roles.get(role_name)
        norm_path = self._state.normalize_path(work_path)
        effective_lock = self._resolve_lock_mode(role, norm_path, lock_mode)
        deps = tuple(d.strip().lstrip("@") for d in (depends_on or ()) if d.strip())

        async with self._dispatch_lock:
            task_id = await self._ticket_store.next_task_id(main_conversation_id)
            self._state.mark_unfinished(main_conversation_id, task_id)

            if self._state.can_acquire(
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                norm_path=norm_path,
                lock_mode=effective_lock,
                depends_on=deps,
            ):
                _ = await self._ticket_store.create_ticket(
                    main_conversation_id=main_conversation_id,
                    task_id=task_id,
                    role_name=role.name,
                    mandate=mandate,
                    work_path=norm_path,
                    lock_mode=effective_lock,
                    depends_on=deps,
                    status="running",
                )
                self._state.acquire_lock(
                    main_conversation_id=main_conversation_id,
                    task_id=task_id,
                    norm_path=norm_path,
                    lock_mode=effective_lock,
                )
                self._spawn_worker(main_conversation_id, task_id)
                return DispatchReceipt(task_id=task_id, status="running", work_path=norm_path)

            _ = await self._ticket_store.create_ticket(
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                role_name=role.name,
                mandate=mandate,
                work_path=norm_path,
                lock_mode=effective_lock,
                depends_on=deps,
                status="queued",
            )
            pos = self._state.enqueue(
                QueuedTask(
                    main_conversation_id=main_conversation_id,
                    task_id=task_id,
                    norm_path=norm_path,
                    lock_mode=effective_lock,
                    depends_on=deps,
                )
            )
            return DispatchReceipt(
                task_id=task_id,
                status="queued",
                queue_position=pos,
                work_path=norm_path,
            )

    async def inspect_workspace_locks(
        self, work_path: str | None = None
    ) -> Sequence[WorkspaceLockStatus]:
        """返回当前活跃工作区读写锁与排队任务快照。"""
        return self._state.inspect(work_path)

    async def steer_or_revise(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        instruction: str,
    ) -> str:
        """运行中实时 Steer 注入、排队中合并指令、终态/失败/中断原链 Revise 续跑。"""
        norm_id = task_id.strip().lstrip("@")
        key = (main_conversation_id, norm_id)
        ticket = await self._ticket_store.get_ticket(main_conversation_id, norm_id)
        if ticket is None:
            raise LookupError(f"未找到任务 @{norm_id}。")

        if ticket.status == "running" and key in self._tasks:
            self._steer_queues.setdefault(key, deque()).append(instruction)
            return f"已向运行中的 @{norm_id} 注入实时纠偏指令（将在下一轮模型请求前生效）。"

        if ticket.status == "queued":
            merged = f"{ticket.mandate}\n\n[追加要求]\n{instruction}"
            _ = await self._ticket_store.update_ticket_status(
                main_conversation_id,
                norm_id,
                status="queued",
                mandate=merged,
            )
            return f"已将追加指令合并到排队中的 @{norm_id}。"

        if ticket.status not in ("completed", "failed", "interrupted"):
            raise ValueError(f"任务 @{norm_id} 当前状态为 {ticket.status}，无法执行 Steer/Revise。")

        norm_path = self._state.normalize_path(ticket.work_path)
        effective_lock = as_lock_mode(ticket.lock_mode)
        deps = parse_depends_on(ticket.depends_on_json)

        async with self._dispatch_lock:
            self._state.mark_unfinished(main_conversation_id, norm_id)
            self._run_prompts[key] = instruction
            if self._state.can_acquire(
                main_conversation_id=main_conversation_id,
                task_id=norm_id,
                norm_path=norm_path,
                lock_mode=effective_lock,
                depends_on=deps,
            ):
                _ = await self._ticket_store.update_ticket_status(
                    main_conversation_id,
                    norm_id,
                    status="running",
                )
                self._state.acquire_lock(
                    main_conversation_id=main_conversation_id,
                    task_id=norm_id,
                    norm_path=norm_path,
                    lock_mode=effective_lock,
                )
                self._spawn_worker(main_conversation_id, norm_id)
                return f"已基于原上下文链重新启动 @{norm_id}（Revise/Retry 运行中）。"

            _ = await self._ticket_store.update_ticket_status(
                main_conversation_id,
                norm_id,
                status="queued",
            )
            pos = self._state.enqueue(
                QueuedTask(
                    main_conversation_id=main_conversation_id,
                    task_id=norm_id,
                    norm_path=norm_path,
                    lock_mode=effective_lock,
                    depends_on=deps,
                )
            )
            return f"@{norm_id} 已重新加入排队队列第 {pos} 位，等待工作区锁或前置依赖完成。"

    async def cancel_task(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
    ) -> str:
        """显式取消任务，并自动解除下游任务对其的依赖、触发后续排队调度。"""
        norm_id = task_id.strip().lstrip("@")
        key = (main_conversation_id, norm_id)
        ticket = await self._ticket_store.get_ticket(main_conversation_id, norm_id)
        if ticket is None:
            raise LookupError(f"未找到任务 @{norm_id}。")

        running_task = self._tasks.get(key)
        if ticket.status == "running" and running_task is not None and not running_task.done():
            _ = running_task.cancel()
            _ = await asyncio.gather(running_task, return_exceptions=True)
            return f"已取消运行中的 @{norm_id}，并自动解除下游对其依赖。"

        if ticket.status in ("completed", "cancelled"):
            return f"任务 @{norm_id} 当前已是 {ticket.status}，无需重复取消。"

        started: list[QueuedTask] = []
        async with self._dispatch_lock:
            _ = self._state.drop_queued(main_conversation_id, norm_id)
            self._state.remove_dependency(main_conversation_id, norm_id)
            _ = await self._ticket_store.update_ticket_status(
                main_conversation_id,
                norm_id,
                status="cancelled",
            )
            await self._ticket_store.remove_dependency(main_conversation_id, norm_id)
            started = await self._start_ready_tasks_locked()

        await self._emit_started(started)
        return f"已取消 @{norm_id}，并自动解除下游对其依赖。"

    async def recover_pending_tasks(
        self,
        main_conversation_id: str,
        *,
        conversation_store: ConversationStore,
    ) -> tuple[list[str], list[str]]:
        """重启恢复：修补中断任务、重建失败/中断阻塞集合，并恢复可执行的排队任务。"""
        if self._step_store is None:
            return [], []
        interrupted_ids, auto_requeue_ids = await self._ticket_store.recover_main_conversation_tasks(
            main_conversation_id,
            conversation_store=conversation_store,
            step_store=self._step_store,
        )
        rows = await self._ticket_store.list_tickets(main_conversation_id)
        started: list[QueuedTask] = []
        async with self._dispatch_lock:
            for row in rows:
                if row.status in ("failed", "interrupted"):
                    self._state.mark_settled(main_conversation_id, row.task_id, blocked=True)
                elif row.status == "queued" and not self._state.is_queued(main_conversation_id, row.task_id):
                    self._state.mark_unfinished(main_conversation_id, row.task_id)
                    _ = self._state.enqueue(
                        QueuedTask(
                            main_conversation_id=main_conversation_id,
                            task_id=row.task_id,
                            norm_path=self._state.normalize_path(row.work_path),
                            lock_mode=as_lock_mode(row.lock_mode),
                            depends_on=parse_depends_on(row.depends_on_json),
                        )
                    )
            if auto_requeue_ids:
                started = await self._start_ready_tasks_locked()

        await self._emit_started(started)
        return interrupted_ids, auto_requeue_ids

    async def list_tasks(self, main_conversation_id: str) -> Sequence[TaskSummary]:
        rows = await self._ticket_store.list_tickets(main_conversation_id)
        return [
            TaskSummary(
                task_id=r.task_id,
                role_name=r.role_name,
                status=r.status,
                work_path=r.work_path,
                mandate=r.mandate,
            )
            for r in rows
        ]

    def _spawn_worker(self, main_conversation_id: str, task_id: str) -> None:
        key = (main_conversation_id, task_id)
        task = asyncio.create_task(
            self._run_worker(main_conversation_id, task_id),
            name=f"worker-{main_conversation_id[:8]}-{task_id}",
        )
        self._tasks[key] = task

    async def _run_worker(self, main_conversation_id: str, task_id: str) -> None:
        key = (main_conversation_id, task_id)
        ticket = await self._ticket_store.get_ticket(main_conversation_id, task_id)
        if ticket is None:
            return
        role = self._roles.get(ticket.role_name)
        steer_queue = self._steer_queues.setdefault(key, deque())
        prompt_text = self._run_prompts.pop(key, ticket.mandate)
        otel_token = otel_context.attach(otel_context.Context())
        blocked_downstream = True
        status_persisted = False
        pending_event: tuple[str, str] | None = None
        perm_hook: PermissionGuardHook | None = None

        try:
            with logfire.span(
                "role_worker {task_id} ({role_name})",
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                role_name=role.name,
                work_path=ticket.work_path,
            ):
                _ = await self._ticket_store.update_ticket_status(main_conversation_id, task_id, status="running")
                role_conv = await self._sessions.load_task_conversation(
                    main_conversation_id=main_conversation_id,
                    task_id=task_id,
                    role_name=role.name,
                    role_instructions=role.instructions,
                )
                extra_caps: list[AbstractCapability[object]] = [
                    TaskSteerHook(steer_queue),
                    ToolExecutionQueueHook(
                        workspace_root=ticket.work_path,
                        coordinator=self._tool_coordinator,
                    ),
                ]
                if self._permission_reviewer is not None:
                    perm_hook = PermissionGuardHook(
                        reviewer=self._permission_reviewer,
                        workspace_root=ticket.work_path,
                        user_intent=prompt_text,
                    )
                    extra_caps.append(perm_hook)
                agent = role.build_agent(
                    step_store=self._step_store,
                    extra_capabilities=extra_caps,
                )
                workspace = LocalWorkspaceBackend(ticket.work_path) if ticket.work_path else None
                result = await agent.run(prompt_text, conversation=role_conv, workspace=workspace)
                await self._sessions.save_task_result(main_conversation_id, result)
                _ = await self._ticket_store.update_ticket_status(
                    main_conversation_id,
                    task_id,
                    status="completed",
                    run_id=result.run_id,
                    result_summary=result.output,
                )
                blocked_downstream = False
                status_persisted = True
                pending_event = (f"@{task_id} (@{role.name}) 已完成:\n{result.output}", "completed")
        except asyncio.CancelledError:
            _ = await self._ticket_store.update_ticket_status(main_conversation_id, task_id, status="cancelled")
            await self._ticket_store.remove_dependency(main_conversation_id, task_id)
            blocked_downstream = False
            status_persisted = True
            pending_event = (f"@{task_id} (@{role.name}) 已取消。", "cancelled")
            raise
        except (AgentRunError, ModelAPIError, UsageLimitExceeded) as exc:
            err_msg = format_worker_error(exc)
            logfire.error(
                "Role worker failed",
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                role_name=role.name,
                error=err_msg,
            )
            _ = await self._ticket_store.update_ticket_status(
                main_conversation_id,
                task_id,
                status="failed",
                result_summary=err_msg,
            )
            status_persisted = True
            pending_event = (
                f"@{task_id} (@{role.name}) 执行失败（需人工 Revise 重试或取消上游）: {err_msg}",
                "failed",
            )
        finally:
            if perm_hook is not None:
                perm_hook.release_runs()
            if not status_persisted:
                _ = await self._ticket_store.update_ticket_status(
                    main_conversation_id,
                    task_id,
                    status="failed",
                    result_summary="Worker terminated unexpectedly.",
                )
                pending_event = (
                    f"@{task_id} (@{role.name}) 意外终止并已标记为 failed。",
                    "failed",
                )
            otel_context.detach(otel_token)
            _ = self._tasks.pop(key, None)
            _ = self._steer_queues.pop(key, None)
            await self._release_and_schedule_next(
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                work_path=ticket.work_path,
                blocked_downstream=blocked_downstream,
                cancelled=not blocked_downstream and not status_persisted,
                completion_event=pending_event,
            )

    async def _release_and_schedule_next(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        work_path: str | None,
        blocked_downstream: bool,
        cancelled: bool = False,
        completion_event: tuple[str, str] | None = None,
    ) -> None:
        started: list[QueuedTask] = []
        async with self._dispatch_lock:
            self._state.mark_settled(main_conversation_id, task_id, blocked=blocked_downstream)
            if not blocked_downstream:
                self._state.remove_dependency(main_conversation_id, task_id)
            if cancelled:
                self._state.remove_dependency(main_conversation_id, task_id)
            self._state.release_lock(
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                norm_path=work_path,
            )
            started = await self._start_ready_tasks_locked()

        if completion_event is not None:
            await self._emit(main_conversation_id, completion_event[0], completion_event[1])
        await self._emit_started(started)

    async def _start_ready_tasks_locked(self) -> list[QueuedTask]:
        started = self._state.pop_ready_tasks()
        for item in started:
            _ = await self._ticket_store.update_ticket_status(
                item.main_conversation_id,
                item.task_id,
                status="running",
            )
            self._spawn_worker(item.main_conversation_id, item.task_id)
        return started

    async def _emit_started(self, started: Sequence[QueuedTask]) -> None:
        for item in started:
            await self._emit(
                item.main_conversation_id,
                f"排队中的 @{item.task_id} 已获取工作区锁({item.lock_mode})并开始执行。",
                "started",
            )

    async def _emit(self, main_conversation_id: str, summary: str, kind: str) -> None:
        if self._on_event is not None:
            _ = await self._on_event(main_conversation_id, summary, kind)

    async def wait_all(self) -> None:
        """等待当前所有后台 Worker 协程结算（含连续出队的排队任务）。"""
        while self._tasks:
            pending = list(self._tasks.values())
            _ = await asyncio.gather(*pending, return_exceptions=True)
