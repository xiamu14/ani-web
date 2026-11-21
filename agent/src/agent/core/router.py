"""Super-Agent 前台 Router 协调器: 即时应答用户、流式推流、中断/插话、工具派发与事件驱动回流。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import logfire
from opentelemetry import context as otel_context
from pydantic_ai import Agent, AgentRunResult, CancellationToken, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.exceptions import (
    RunCancelled,
)
from pydantic_ai.messages import TextContent, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai_harness.ask_user import AskUser, AskUserRequest, AskUserResponse
from pydantic_ai_harness.step_persistence import StepPersistence, StepStore

from agent.attachments.ports import AttachmentRegistryPort
from agent.context import ContextTreeManager, SemanticBranchRouter
from agent.core.budget import ROUTER_BUDGET_POLICY, LoopBudgetPolicy
from agent.db import SQLModelConversationStore
from agent.hooks import (
    RouterContextHook,
    RouterSteerHook,
    RunErrorReportingHook,
    SemanticRoutingHook,
    WorkspaceMcpPort,
    format_exception_detail,
    sort_capabilities,
)
from agent.protocol.event_bus import RuntimeEventBus
from agent.protocol.events_schema import (
    RunCompletedPayload,
    RunFailedPayload,
    RunInterruptedPayload,
    RunStartedPayload,
    TreeBranchSwitchedPayload,
)
from agent.protocol.interactions import InteractionBroker
from agent.protocol.stream_bridge import (
    make_event_stream_handler,
    to_run_usage_view,
    verify_citations_for_protocol,
)
from agent.tools import (
    RouterDeps,
    TaskDispatchPort,
    ToolExecutionQueueHook,
    ToolLockCoordinator,
    build_router_toolset,
)

DEFAULT_ROUTER_INSTRUCTIONS = """\
你是全局协调器（Router），负责直接与用户对话并协调后台 Role Agents。
核心原则：
1. 轻量化与零阻塞：你可以直接回答普通问题；耗时的代码修改、调研、测试等任务一律交给对应 Role（如 `coder`、`reviewer`、`tester`、`researcher`）后台执行，派发/复用后立即向用户反馈 `@task_id`，不等待后台完成。**同主题同角色优先复用**：若当前对话中已存在同一主题且同一角色的已有任务（无论是 `completed`/`failed`/`interrupted` 还是 `running`/`queued`，不确定时可先 `list_tasks` 查看），优先通过 `steer_or_revise_task` 复用该 `@task_id` 续跑，避免重复新建；仅在新主题、切换角色或需并行任务时才调用 `dispatch_role_agent`。
2. 工作区读写锁与依赖规划：
   - 调度器自动对同目录（含父子目录重叠）执行读写锁：`coder`/`tester` 为写锁（exclusive），`researcher`/`reviewer` 为共享读锁（shared），只读任务之间自动并发、写锁与读写均互斥防饥饿。
   - 若用户明确要求先后顺序（如“先让 coder 改完再让 researcher 总结”），请先派发前置任务拿到 `@task_id`，再传 `depends_on=["T1"]` 派发后续任务；需要了解当前锁占用时可调用 `inspect_workspace_locks`。
3. 工作区管理与能力目录：当用户提及具体目录或项目路径时，调用 `set_workspace` 绑定当前会话工作区。MCP 工具与 Skills 按当前工作区动态挂载在后台 Role Agent 上，Router 自身不直接持有其 Tool Schema；请求末尾的 `<workspace_capabilities>` 列出了当前工作区可用的 MCP 与 Skill 轻目录。用户引用 `@mcp:<name>` 或 Skill 时，对照 `<workspace_capabilities>` 确认存在后直接通过 `dispatch_role_agent` 派发给对应 Role（如 `researcher`/`coder`）执行，若目录中确实不存在再如实提示。
4. @task_id 三级消歧与上下游失败处理：
   - 对后台任务执行 `steer_or_revise_task` 时：
     a) 用户显式引用 `@T1` 等 ID，传 `user_confirmed=True` 直接执行；
     b) 能根据任务主题/目标（mandate/role/work_path）**明确唯一识别**目标任务时，无需 `ask_user_question`，直接传 `user_confirmed=True` 执行；
     c) 后台**唯一存在 1 个**未完成/失败/中断任务（`running`/`queued`/`failed`/`interrupted`）且用户意图明确为继续、重试或追加修订时，无需 `ask_user_question`，直接传 `user_confirmed=True` 执行；
     d) 仅当存在多个候选任务且主题无法明确区分时，才先 `list_tasks` 或用 `ask_user_question` 请用户选择。
   - 对后台任务执行破坏性的 `cancel_task` 时：用户显式引用 `@T1` 等 ID 传 `user_confirmed=True` 直接执行；未显式引用时先传 `user_confirmed=False`（或 `ask_user_question`）触发确认。
   - 若上游任务 `failed` / `interrupted`，下游依赖任务保持 `queued` 等待；禁止绕过下游或替用户自动重跑，只能引导用户对上游执行 `steer_or_revise_task`（修正重试）或 `cancel_task`（取消上游并自动放行下游）。
"""

ProactiveReplyHandler = Callable[[str, str], Awaitable[None]]


def _extract_prompt_text(prompt: str | Sequence[UserContent]) -> str:
    if isinstance(prompt, str):
        return prompt
    parts: list[str] = []
    for item in prompt:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, TextContent):
            parts.append(item.content)
    return "\n".join(parts)


@dataclass(frozen=True, kw_only=True)
class RouterReply:
    """Router 单次交互的返回结果。"""

    conversation_id: str
    output: str
    run_id: str = ""
    interrupted: bool = False


class SuperAgentRouter:
    """前台协调器：装配 Hooks 与工具集，处理用户消息、流式应答、中断插话与后台事件。"""

    def __init__(
        self,
        *,
        model: Model | KnownModelName | str,
        context_tree: ContextTreeManager,
        conversation_store: SQLModelConversationStore,
        task_dispatcher: TaskDispatchPort | None = None,
        step_store: StepStore | None = None,
        semantic_router: SemanticBranchRouter | None = None,
        instructions: str = DEFAULT_ROUTER_INSTRUCTIONS,
        on_proactive_reply: ProactiveReplyHandler | None = None,
        tool_coordinator: ToolLockCoordinator | None = None,
        budget_policy: LoopBudgetPolicy = ROUTER_BUDGET_POLICY,
        event_bus: RuntimeEventBus | None = None,
        interaction_broker: InteractionBroker | None = None,
        attachment_registry: AttachmentRegistryPort | None = None,
        mcp_manager: WorkspaceMcpPort | None = None,
        extra_capabilities: Sequence[AbstractCapability[RouterDeps]] = (),
    ) -> None:
        self._context_tree: ContextTreeManager = context_tree
        self._store: SQLModelConversationStore = conversation_store
        self._tasks: TaskDispatchPort | None = task_dispatcher
        self._on_proactive_reply: ProactiveReplyHandler | None = on_proactive_reply
        self._budget_policy: LoopBudgetPolicy = budget_policy
        self._event_bus: RuntimeEventBus | None = event_bus
        self._interaction_broker: InteractionBroker | None = interaction_broker
        self._attachments: AttachmentRegistryPort | None = attachment_registry
        self._pending_events: dict[str, deque[str]] = {}
        self._steer_queues: dict[str, deque[str]] = {}
        self._running_conversations: set[str] = set()
        self._active_cancel_tokens: dict[str, CancellationToken] = {}
        self._active_run_ids: dict[str, str] = {}
        self._conv_locks: dict[str, asyncio.Lock] = {}
        self._routing_hook: SemanticRoutingHook = SemanticRoutingHook(
            context_tree, semantic_router=semantic_router
        )

        capabilities: list[AbstractCapability[RouterDeps]] = [
            RouterContextHook(self._pending_events, self._context_tree, mcp_manager=mcp_manager),
            RouterSteerHook(self._steer_queues),
            RunErrorReportingHook(agent_name="router"),
            cast(
                AbstractCapability[RouterDeps],
                ToolExecutionQueueHook(coordinator=tool_coordinator),
            ),
            *extra_capabilities,
        ]
        if interaction_broker is not None:

            async def _router_ask_user(
                ctx: RunContext[RouterDeps], req: AskUserRequest
            ) -> AskUserResponse:
                return await interaction_broker.answer_ask_user(
                    req,
                    conversation_id=ctx.deps.main_conversation_id,
                    task_id=None,
                    role_name="router",
                    run_id=ctx.run_id or None,
                )

            capabilities.append(
                cast(
                    AbstractCapability[RouterDeps],
                    AskUser[RouterDeps](answerer=_router_ask_user),
                )
            )
        if step_store is not None:
            capabilities.append(
                StepPersistence(store=step_store, agent_name="router", capture_frontier=True)
            )

        self._agent: Agent[RouterDeps, str] = Agent(
            model=model,
            deps_type=RouterDeps,
            name="router",
            instructions=instructions,
            toolsets=[build_router_toolset()],
            capabilities=sort_capabilities(capabilities),
            retries=budget_policy.max_retries,
        )

    def _conv_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._conv_locks.setdefault(conversation_id, asyncio.Lock())

    def is_running(self, conversation_id: str) -> bool:
        return conversation_id in self._running_conversations

    def steer_conversation(self, conversation_id: str, instruction: str) -> bool:
        """向正在运行的 Router 会话实时注入用户建议（下一轮请求前生效）。"""
        if conversation_id not in self._running_conversations:
            return False
        self._steer_queues.setdefault(conversation_id, deque()).append(instruction)
        return True

    async def interrupt_conversation(
        self,
        conversation_id: str,
        *,
        reason: Literal["user_interrupt", "shutdown", "task_cancelled"] = "user_interrupt",
    ) -> tuple[bool, str | None, str | None]:
        """优雅中断当前运行中的 Router 生成并清理挂起交互。"""
        _ = reason
        token = self._active_cancel_tokens.get(conversation_id)
        run_id = self._active_run_ids.get(conversation_id)
        if token is None:
            return False, None, None
        token.cancel()
        if self._interaction_broker is not None:
            _ = self._interaction_broker.cancel_for_scope(
                run_id=run_id,
                conversation_id=conversation_id,
                reason="run_cancelled",
            )
        # 等待当前持锁 run 完成修复落盘
        async with self._conv_lock(conversation_id):
            rec = await self._store.read_record(conversation_id)
            return True, run_id, rec.head_id if rec is not None else None

    async def switch_conversation(self, conversation_id: str) -> Conversation:
        """用户显式指令切换分支（仅切换已有会话，绝不新建）。"""
        prev = self._context_tree.active_conversation_id
        conv = await self._context_tree.switch_conversation(conversation_id)
        if self._event_bus is not None and prev != conversation_id:
            _ = self._event_bus.publish(
                "tree.branch_switched",
                TreeBranchSwitchedPayload(
                    previous_conversation_id=prev,
                    active_conversation_id=conversation_id,
                    reason="user_switch",
                ),
                conversation_id=conversation_id,
            )
        return conv

    async def handle_user_message(
        self,
        prompt: str | Sequence[UserContent],
        *,
        conversation_id: str | None = None,
        work_path: str | Path | None = None,
        routing_mode: Literal["auto", "pinned"] = "auto",
        delivery_mode: Literal["auto", "steer", "queue", "interrupt_and_send"] = "auto",
        run_id: str | None = None,
    ) -> RouterReply:
        """经前置语义路由解析目标分支后，执行流式/非流式对话并落盘。"""
        prompt_text = _extract_prompt_text(prompt)
        if routing_mode == "pinned" and conversation_id is not None:
            conv = await self._context_tree.switch_conversation(conversation_id)
        else:
            conv = await self._routing_hook.resolve_conversation(
                prompt_text, conversation_id=conversation_id, work_path=work_path
            )
        target_id = conv.conversation_id

        if target_id in self._running_conversations:
            if delivery_mode == "steer":
                _ = self.steer_conversation(target_id, prompt_text)
                return RouterReply(
                    conversation_id=target_id,
                    output="",
                    run_id=self._active_run_ids.get(target_id, ""),
                )
            if delivery_mode == "interrupt_and_send":
                _ = await self.interrupt_conversation(target_id)

        effective_run_id = run_id or f"run_{uuid4().hex[:12]}"
        async with self._conv_lock(target_id):
            latest_conv = await self._context_tree.load_conversation(target_id)
            start_rec = await self._store.read_record(target_id)
            deps = RouterDeps(
                main_conversation_id=target_id, workspace=self._context_tree, tasks=self._tasks
            )
            cancel_token = CancellationToken()
            self._running_conversations.add(target_id)
            self._active_cancel_tokens[target_id] = cancel_token
            self._active_run_ids[target_id] = effective_run_id
            if self._event_bus is not None:
                _ = self._event_bus.publish(
                    "run.started",
                    RunStartedPayload(
                        agent_kind="router", proactive=False,
                        entry_id=start_rec.head_id if start_rec is not None else None,
                        user_message_preview=prompt_text[:160],
                    ),
                    conversation_id=target_id, role_name="router", run_id=effective_run_id,
                )
            stream_handler = (
                make_event_stream_handler(
                    self._event_bus, conversation_id=target_id, role_name="router", proactive=False
                )
                if self._event_bus is not None
                else None
            )
            try:
                result = await self._agent.run(
                    prompt, conversation=latest_conv, deps=deps, run_id=effective_run_id,
                    cancellation_token=cancel_token, event_stream_handler=stream_handler,
                    usage_limits=self._budget_policy.to_usage_limits(base_usage=latest_conv.usage),
                    timeout=self._budget_policy.wall_clock_timeout_sec,
                )
            except RunCancelled:
                repaired = await self._context_tree.repair_interrupted_conversation(target_id)
                rec = await self._store.read_record(target_id)
                head_id = rec.head_id if rec is not None else (start_rec.head_id if start_rec else None)
                if self._event_bus is not None:
                    _ = self._event_bus.publish(
                        "run.interrupted",
                        RunInterruptedPayload(
                            reason="user_interrupt",
                            repaired_message_count=1 if repaired else 0,
                            head_entry_id=head_id,
                        ),
                        conversation_id=target_id, role_name="router", run_id=effective_run_id,
                    )
                return RouterReply(conversation_id=target_id, output="", run_id=effective_run_id, interrupted=True)
            except Exception as exc:  # 播报兜底：统一捕获并记录所有运行时异常
                if self._event_bus is not None:
                    _ = self._event_bus.publish(
                        "run.failed",
                        RunFailedPayload(
                            error_code=type(exc).__name__,
                            error_message=format_exception_detail(exc),
                        ),
                        conversation_id=target_id, role_name="router", run_id=effective_run_id,
                    )
                exc.add_note("_run_failed_emitted")
                raise
            finally:
                self._running_conversations.discard(target_id)
                _ = self._active_cancel_tokens.pop(target_id, None)
                _ = self._active_run_ids.pop(target_id, None)

            await self._save_result(target_id, result)
            if self._event_bus is not None:
                rec = await self._store.read_record(target_id)
                citations = verify_citations_for_protocol(result.output, self._attachments)
                _ = self._event_bus.publish(
                    "run.completed",
                    RunCompletedPayload(
                        output_text=result.output, citations=citations,
                        usage=to_run_usage_view(result.usage, budget_policy=self._budget_policy),
                        head_entry_id=rec.head_id if rec is not None else None, proactive=False,
                    ),
                    conversation_id=target_id, role_name="router", run_id=effective_run_id,
                )
            await self._drain_pending_events(target_id)
            return RouterReply(conversation_id=target_id, output=result.output, run_id=effective_run_id)

    def enqueue_system_notice(
        self, main_conversation_id: str, summary: str, *, kind: str = "recovery"
    ) -> None:
        """在启动恢复等场景下将系统提示注入待投递事件队列，于下次对话时自动告知 Router。"""
        report = f'<ticket_report kind="{kind}">\n{summary}\n</ticket_report>'
        self._pending_events.setdefault(main_conversation_id, deque()).append(report)

    async def on_ticket_event(
        self,
        main_conversation_id: str,
        summary: str,
        *,
        kind: str,
    ) -> str | None:
        """接收后台任务事件：主会话运行中则实时注入，空闲且为终态时唤醒一次轻量汇报。"""
        report = f'<ticket_report kind="{kind}">\n{summary}\n</ticket_report>'
        if main_conversation_id in self._running_conversations:
            self._pending_events.setdefault(main_conversation_id, deque()).append(report)
            return None

        if kind not in ("completed", "failed"):
            return None

        async with self._conv_lock(main_conversation_id):
            latest = await self._run_proactive_report(main_conversation_id, report)
            await self._drain_pending_events(main_conversation_id)
            return latest

    async def _run_proactive_report(self, conversation_id: str, report: str) -> str:
        conv = await self._context_tree.load_conversation(conversation_id)
        deps = RouterDeps(
            main_conversation_id=conversation_id,
            workspace=self._context_tree,
            tasks=self._tasks,
        )
        proactive_run_id = f"run_{uuid4().hex[:12]}"
        self._running_conversations.add(conversation_id)
        otel_token = otel_context.attach(otel_context.Context())
        if self._event_bus is not None:
            _ = self._event_bus.publish(
                "run.started",
                RunStartedPayload(agent_kind="router", proactive=True, user_message_preview="后台任务进展播报"),
                conversation_id=conversation_id, role_name="router", run_id=proactive_run_id,
            )
        stream_handler = (
            make_event_stream_handler(
                self._event_bus,
                conversation_id=conversation_id,
                role_name="router",
                proactive=True,
            )
            if self._event_bus is not None
            else None
        )
        try:
            with logfire.span(
                "router proactive_report",
                main_conversation_id=conversation_id,
            ):
                result = await self._agent.run(
                    f"后台任务有新进展，请向用户简洁播报结果：\n{report}",
                    conversation=conv,
                    deps=deps,
                    run_id=proactive_run_id,
                    event_stream_handler=stream_handler,
                    usage_limits=self._budget_policy.to_usage_limits(base_usage=conv.usage),
                    timeout=self._budget_policy.wall_clock_timeout_sec,
                )
        except Exception as exc:  # noqa: BLE001  # 播报兜底：统一捕获并记录所有运行时异常
            logfire.error(
                "Router proactive report failed; falling back to raw ticket summary",
                main_conversation_id=conversation_id,
                error=format_exception_detail(exc),
            )
            fallback_output = f"[后台任务通知]\n{report}"
            if self._event_bus is not None:
                _ = self._event_bus.publish(
                    "run.completed",
                    RunCompletedPayload(output_text=fallback_output, proactive=True),
                    conversation_id=conversation_id,
                    role_name="router",
                    run_id=proactive_run_id,
                )
            if self._on_proactive_reply is not None:
                await self._on_proactive_reply(conversation_id, fallback_output)
            return fallback_output
        finally:
            otel_context.detach(otel_token)
            self._running_conversations.discard(conversation_id)

        await self._save_result(conversation_id, result)
        if self._event_bus is not None:
            rec = await self._store.read_record(conversation_id)
            _ = self._event_bus.publish(
                "run.completed",
                RunCompletedPayload(
                    output_text=result.output,
                    usage=to_run_usage_view(
                        result.usage, budget_policy=self._budget_policy
                    ),
                    head_entry_id=rec.head_id if rec is not None else None,
                    proactive=True,
                ),
                conversation_id=conversation_id,
                role_name="router",
                run_id=proactive_run_id,
            )
        if self._on_proactive_reply is not None:
            await self._on_proactive_reply(conversation_id, result.output)
        return result.output

    async def _drain_pending_events(self, conversation_id: str) -> None:
        """排空主会话运行期间新到达但未被 Hook 消费的后台事件，避免遗漏播报。"""
        queue = self._pending_events.get(conversation_id)
        while queue:
            batch: list[str] = []
            while queue:
                item = queue.popleft()
                if 'kind="completed"' in item or 'kind="failed"' in item:
                    batch.append(item)
            if not batch:
                break
            _ = await self._run_proactive_report(conversation_id, "\n".join(batch))

    async def _save_result(self, conversation_id: str, result: AgentRunResult[str]) -> None:
        try:
            _ = await self._store.save(result, scope="main")
        except ConversationConflict:
            latest = await self._store.load(conversation_id, scope="main")
            if latest is None:
                raise
            latest.messages.extend(result.new_messages())
            _ = await self._store.save(latest, scope="main")
