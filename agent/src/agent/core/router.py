"""Super-Agent 前台 Router 协调器: 即时应答用户、工具派发与事件驱动回流。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import logfire
from opentelemetry import context as otel_context
from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.exceptions import AgentRunError, ModelAPIError, UsageLimitExceeded
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai_harness.step_persistence import StepPersistence, StepStore

from agent.context import ContextTreeManager, SemanticBranchRouter
from agent.core.budget import ROUTER_BUDGET_POLICY, LoopBudgetPolicy
from agent.db import SQLModelConversationStore
from agent.hooks import RouterContextHook, SemanticRoutingHook, sort_capabilities
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
1. 轻量化与零阻塞：你可以直接回答普通问题；耗时的代码修改、调研、测试等任务一律通过 `dispatch_role_agent` 派发给对应角色（如 coder、reviewer、tester、researcher），派发后立即向用户反馈 @task_id，不等待后台完成。
2. 工作区读写锁与依赖规划：
   - 调度器自动对同目录（含父子目录重叠）执行读写锁：`coder`/`tester` 为写锁（exclusive），`researcher`/`reviewer` 为共享读锁（shared），只读任务之间自动并发、写锁与读写均互斥防饥饿。
   - 若用户明确要求先后顺序（如“先让 coder 改完再让 researcher 总结”），请先派发前置任务拿到 `@task_id`，再传 `depends_on=["T1"]` 派发后续任务；需要了解当前锁占用时可调用 `inspect_workspace_locks`。
3. 工作区管理：当用户提及具体目录或项目路径时，调用 `set_workspace` 绑定当前会话工作区。
4. @task_id 三级消歧与上下游失败处理：
   - 对后台任务执行 `steer_or_revise_task` 或 `cancel_task` 时：用户显式引用 @T1 等 ID 时传 `user_confirmed=True` 直接执行；未引用但能唯一推断目标时先传 `user_confirmed=False` 触发确认；存在多个候选时先 `list_tasks` 请用户选择。
   - 若上游任务 `failed` / `interrupted`，下游依赖任务保持 `queued` 等待；禁止绕过下游或替用户自动重跑，只能引导用户对上游执行 `steer_or_revise_task`（修正重试）或 `cancel_task`（取消上游并自动放行下游）。
"""

ProactiveReplyHandler = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class RouterReply:
    """Router 单次交互的返回结果。"""

    conversation_id: str
    output: str


class SuperAgentRouter:
    """前台协调器：装配 Hooks 与工具集，处理用户消息与后台事件。"""

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
    ) -> None:
        self._context_tree: ContextTreeManager = context_tree
        self._store: SQLModelConversationStore = conversation_store
        self._tasks: TaskDispatchPort | None = task_dispatcher
        self._on_proactive_reply: ProactiveReplyHandler | None = on_proactive_reply
        self._budget_policy: LoopBudgetPolicy = budget_policy
        self._pending_events: dict[str, deque[str]] = {}
        self._running_conversations: set[str] = set()
        self._conv_locks: dict[str, asyncio.Lock] = {}
        self._routing_hook: SemanticRoutingHook = SemanticRoutingHook(
            context_tree, semantic_router=semantic_router
        )

        capabilities: list[AbstractCapability[RouterDeps]] = [
            RouterContextHook(self._pending_events, self._context_tree),
            cast(AbstractCapability[RouterDeps], ToolExecutionQueueHook(coordinator=tool_coordinator)),
        ]
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

    async def switch_conversation(self, conversation_id: str) -> Conversation:
        """用户显式指令切换分支（仅切换已有会话，绝不新建）。"""
        return await self._context_tree.switch_conversation(conversation_id)

    async def handle_user_message(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        work_path: str | Path | None = None,
    ) -> RouterReply:
        """经前置语义路由 Hook 解析目标分支后，立即执行对话并落盘。"""
        conv = await self._routing_hook.resolve_conversation(
            prompt, conversation_id=conversation_id, work_path=work_path
        )
        target_id = conv.conversation_id
        async with self._conv_lock(target_id):
            latest_conv = await self._context_tree.load_conversation(target_id)
            deps = RouterDeps(
                main_conversation_id=target_id,
                workspace=self._context_tree,
                tasks=self._tasks,
            )
            self._running_conversations.add(target_id)
            try:
                result = await self._agent.run(
                    prompt,
                    conversation=latest_conv,
                    deps=deps,
                    usage_limits=self._budget_policy.to_usage_limits(),
                    timeout=self._budget_policy.wall_clock_timeout_sec,
                )
            finally:
                self._running_conversations.discard(target_id)
            await self._save_result(target_id, result)
            await self._drain_pending_events(target_id)
            return RouterReply(conversation_id=target_id, output=result.output)

    def enqueue_system_notice(self, main_conversation_id: str, summary: str, *, kind: str = "recovery") -> None:
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
        self._running_conversations.add(conversation_id)
        otel_token = otel_context.attach(otel_context.Context())
        try:
            with logfire.span(
                "router proactive_report",
                main_conversation_id=conversation_id,
            ):
                result = await self._agent.run(
                    f"后台任务有新进展，请向用户简洁播报结果：\n{report}",
                    conversation=conv,
                    deps=deps,
                    usage_limits=self._budget_policy.to_usage_limits(),
                    timeout=self._budget_policy.wall_clock_timeout_sec,
                )
        except (AgentRunError, ModelAPIError, UsageLimitExceeded, TimeoutError) as exc:
            logfire.error(
                "Router proactive report failed; falling back to raw ticket summary",
                main_conversation_id=conversation_id,
                error=str(exc),
            )
            fallback_output = f"[后台任务通知]\n{report}"
            if self._on_proactive_reply is not None:
                await self._on_proactive_reply(conversation_id, fallback_output)
            return fallback_output
        finally:
            otel_context.detach(otel_token)
            self._running_conversations.discard(conversation_id)
        await self._save_result(conversation_id, result)
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
