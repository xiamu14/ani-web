"""Layer 1 Role Task 实时进度追踪与协议事件发射桥接器（解耦调度器与 SSE 总线）。"""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.messages import AgentStreamEvent

from agent.core.budget import LoopBudgetPolicy
from agent.core.lock_queue import LockQueueState
from agent.core.outcome_judge import ToolExecutionTrace
from agent.core.roles import RoleRegistry
from agent.db.models import TicketRow
from agent.db.ticket_store import parse_depends_on
from agent.protocol.event_bus import RuntimeEventBus
from agent.protocol.events_schema import (
    RunCompletedPayload,
    RunFailedPayload,
    RunInterruptedPayload,
    RunStartedPayload,
    TaskCreatedPayload,
    TaskProgressPayload,
    TaskStatusChangedPayload,
    TaskSteeredPayload,
    WorkspaceLocksChangedPayload,
)
from agent.protocol.interactions import InteractionBroker
from agent.protocol.models import (
    ActiveToolView,
    TaskLivePhaseLiteral,
    TaskStatusLiteral,
    TaskView,
    ToolStepSummaryView,
    WorkspaceLockView,
)
from agent.protocol.stream_bridge import make_event_stream_handler


@dataclass
class TaskLiveState:
    """单个 Role Task 的内存瞬时运行态。"""

    live_phase: TaskLivePhaseLiteral = "settled"
    current_step: int = 0
    tool_calls_count: int = 0
    soft_limit_triggered: bool = False
    active_tool: ActiveToolView | None = None
    recent_steps: list[ToolStepSummaryView] = field(default_factory=list)


class TaskProtocolBridge:
    """将 WorkspaceLockManager 的任务/锁状态变更转换为协议层 TaskView 与 SSE 事件。"""

    def __init__(
        self,
        *,
        event_bus: RuntimeEventBus | None = None,
        interaction_broker: InteractionBroker | None = None,
        lock_state: LockQueueState,
        roles: RoleRegistry,
    ) -> None:
        self._bus: RuntimeEventBus | None = event_bus
        self._broker: InteractionBroker | None = interaction_broker
        self._lock_state: LockQueueState = lock_state
        self._roles: RoleRegistry = roles
        self._live: dict[tuple[str, str], TaskLiveState] = {}

    def get_live(self, main_conversation_id: str, task_id: str) -> TaskLiveState:
        return self._live.setdefault((main_conversation_id, task_id), TaskLiveState())

    def to_task_view(
        self,
        row: TicketRow,
        *,
        queue_position: int = 0,
    ) -> TaskView:
        key = (row.main_conversation_id, row.task_id)
        live = self._live.get(key)
        deps = list(parse_depends_on(row.depends_on_json))
        policy = self._effective_policy(row.role_name)
        phase: TaskLivePhaseLiteral
        if live is not None:
            phase = live.live_phase
        elif row.status == "queued":
            phase = "waiting_dependency" if deps else "acquiring_lock"
        elif row.status == "running":
            phase = "thinking"
        else:
            phase = "settled"

        pending_ids = (
            self._broker.pending_ids_for_task(row.main_conversation_id, row.task_id)
            if self._broker is not None
            else []
        )
        lock_mode: Literal["exclusive", "shared", "none"] = (
            row.lock_mode
            if row.lock_mode in ("exclusive", "shared", "none")
            else "exclusive"
        )
        status: TaskStatusLiteral = (
            row.status
            if row.status
            in ("queued", "running", "completed", "failed", "cancelled", "interrupted")
            else "failed"
        )
        return TaskView(
            main_conversation_id=row.main_conversation_id,
            task_id=row.task_id,
            role_name=row.role_name,
            role_conversation_id=row.role_conversation_id,
            mandate=row.mandate,
            work_path=row.work_path,
            lock_mode=lock_mode,
            status=status,
            live_phase=phase,
            queue_position=queue_position,
            depends_on=deps,
            blocked_by=deps if row.status == "queued" else [],
            pending_interaction_ids=pending_ids,
            current_step=live.current_step if live is not None else 0,
            max_requests=policy.max_requests,
            tool_calls_count=live.tool_calls_count if live is not None else 0,
            max_tool_calls=policy.max_tool_calls,
            soft_limit_triggered=live.soft_limit_triggered if live is not None else False,
            active_tool=live.active_tool if live is not None else None,
            recent_steps=list(live.recent_steps) if live is not None else [],
            result_summary=row.result_summary,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _effective_policy(self, role_name: str) -> LoopBudgetPolicy:
        try:
            return self._roles.get(role_name).effective_budget_policy()
        except LookupError:
            return LoopBudgetPolicy()

    def emit_created(self, row: TicketRow, *, queue_position: int = 0) -> None:
        live = self.get_live(row.main_conversation_id, row.task_id)
        if row.status == "running":
            live.live_phase = "thinking"
        else:
            deps = parse_depends_on(row.depends_on_json)
            live.live_phase = "waiting_dependency" if deps else "acquiring_lock"
        if self._bus is None:
            return
        view = self.to_task_view(row, queue_position=queue_position)
        _ = self._bus.publish(
            "task.created",
            TaskCreatedPayload(task=view),
            conversation_id=row.main_conversation_id,
            task_id=row.task_id,
            role_name=row.role_name,
        )

    def emit_status_changed(
        self,
        row: TicketRow,
        *,
        previous_status: TaskStatusLiteral | None = None,
        previous_phase: TaskLivePhaseLiteral | None = None,
    ) -> None:
        live = self.get_live(row.main_conversation_id, row.task_id)
        if row.status == "running":
            live.live_phase = "thinking"
        elif row.status in ("completed", "failed", "cancelled", "interrupted"):
            live.live_phase = "settled"
            live.active_tool = None
        if self._bus is None:
            return
        view = self.to_task_view(row)
        _ = self._bus.publish(
            "task.status_changed",
            TaskStatusChangedPayload(
                task=view,
                previous_status=previous_status,
                previous_phase=previous_phase,
            ),
            conversation_id=row.main_conversation_id,
            task_id=row.task_id,
            role_name=row.role_name,
        )

    def update_progress(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        role_name: str,
        run_id: str | None = None,
        live_phase: TaskLivePhaseLiteral | None = None,
        active_tool: ActiveToolView | None = None,
        clear_active_tool: bool = False,
        recent_step: ToolStepSummaryView | None = None,
        current_step: int | None = None,
        tool_calls_count: int | None = None,
        soft_limit_triggered: bool | None = None,
    ) -> None:
        live = self.get_live(main_conversation_id, task_id)
        if live_phase is not None:
            live.live_phase = live_phase
        if clear_active_tool:
            live.active_tool = None
        elif active_tool is not None:
            live.active_tool = active_tool
        if recent_step is not None:
            live.recent_steps.append(recent_step)
            if len(live.recent_steps) > 10:
                live.recent_steps = live.recent_steps[-10:]
        if current_step is not None:
            live.current_step = current_step
        if tool_calls_count is not None:
            live.tool_calls_count = tool_calls_count
        if soft_limit_triggered is not None:
            live.soft_limit_triggered = soft_limit_triggered

        if self._bus is None:
            return
        policy = self._effective_policy(role_name)
        _ = self._bus.publish(
            "task.progress",
            TaskProgressPayload(
                task_id=task_id,
                live_phase=live.live_phase,
                current_step=live.current_step,
                max_requests=policy.max_requests,
                tool_calls_count=live.tool_calls_count,
                max_tool_calls=policy.max_tool_calls,
                soft_limit_triggered=live.soft_limit_triggered,
                active_tool=live.active_tool,
                recent_step=recent_step,
            ),
            conversation_id=main_conversation_id,
            task_id=task_id,
            role_name=role_name,
            run_id=run_id,
        )

    def emit_steered(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        role_name: str,
        mode: Literal["live_steer", "queued_merge", "revise_restart"],
        instruction: str,
    ) -> None:
        if self._bus is None:
            return
        _ = self._bus.publish(
            "task.steered",
            TaskSteeredPayload(
                task_id=task_id,
                mode=mode,
                instruction_preview=instruction[:160],
            ),
            conversation_id=main_conversation_id,
            task_id=task_id,
            role_name=role_name,
        )

    def emit_locks_changed(self) -> None:
        if self._bus is None:
            return
        snapshots = self._lock_state.inspect()
        views = [
            WorkspaceLockView(
                work_path=s.work_path,
                exclusive_holder=s.exclusive_holder,
                shared_holders=list(s.shared_holders),
                queued_tasks=list(s.queued_tasks),
            )
            for s in snapshots
        ]
        _ = self._bus.publish(
            "workspace.locks_changed",
            WorkspaceLocksChangedPayload(locks=views),
        )

    def start_worker_stream(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        role_name: str,
        run_id: str,
        prompt_text: str,
        trace: ToolExecutionTrace,
    ) -> Callable[[RunContext[object], AsyncIterable[AgentStreamEvent]], Coroutine[object, object, None]] | None:
        if self._bus is None:
            return None
        _ = self._bus.publish(
            "run.started",
            RunStartedPayload(agent_kind="role", user_message_preview=prompt_text[:160]),
            conversation_id=main_conversation_id,
            task_id=task_id,
            role_name=role_name,
            run_id=run_id,
            channel="task_stream",
        )
        raw = make_event_stream_handler(
            self._bus,
            conversation_id=main_conversation_id,
            task_id=task_id,
            role_name=role_name,
            channel="task_stream",
            on_tool_call=lambda cid, tname, ap: self.update_progress(
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                role_name=role_name,
                run_id=run_id,
                live_phase="calling_tool",
                tool_calls_count=len(trace.records) + 1,
                active_tool=ActiveToolView(
                    tool_call_id=cid, tool_name=tname, args_preview=ap, started_at=""
                ),
            ),
            on_tool_result=lambda tname, ok, err: self.update_progress(
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                role_name=role_name,
                run_id=run_id,
                live_phase="thinking",
                clear_active_tool=True,
                recent_step=ToolStepSummaryView(
                    tool_name=tname, succeeded=ok, error_preview=err
                ),
            ),
        )
        return raw  # pyright: ignore[reportReturnType]

    def emit_worker_outcome(
        self,
        *,
        row: TicketRow,
        run_id: str,
        outcome: Literal["completed", "failed", "cancelled"],
        output_text: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        self.emit_status_changed(row)
        if self._bus is None:
            return
        if outcome == "completed":
            _ = self._bus.publish(
                "run.completed", RunCompletedPayload(output_text=output_text),
                conversation_id=row.main_conversation_id, task_id=row.task_id,
                role_name=row.role_name, run_id=run_id, channel="task_stream",
            )
        elif outcome == "cancelled":
            _ = self._bus.publish(
                "run.interrupted", RunInterruptedPayload(reason="task_cancelled"),
                conversation_id=row.main_conversation_id, task_id=row.task_id,
                role_name=row.role_name, run_id=run_id, channel="task_stream",
            )
        else:
            _ = self._bus.publish(
                "run.failed", RunFailedPayload(error_code=error_code, error_message=error_message),
                conversation_id=row.main_conversation_id, task_id=row.task_id,
                role_name=row.role_name, run_id=run_id, channel="task_stream",
            )
