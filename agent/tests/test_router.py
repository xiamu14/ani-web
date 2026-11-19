"""SuperAgentRouter 单元测试: 即时对话、工具派发与事件驱动回流。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from agent.context import ContextTreeManager
from agent.core import SuperAgentRouter
from agent.db import (
    SQLModelConversationStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)
from agent.tools import DispatchReceipt, LockMode, TaskSummary, WorkspaceLockStatus


class _FakeDispatcher:
    def __init__(self) -> None:
        self.items: list[TaskSummary] = []

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
        _ = (main_conversation_id, lock_mode, depends_on)
        task_id = f"T{len(self.items) + 1}"
        self.items.append(
            TaskSummary(task_id=task_id, role_name=role_name, status="running", work_path=work_path, mandate=mandate)
        )
        return DispatchReceipt(task_id=task_id, status="running", work_path=work_path)

    async def steer_or_revise(self, *, main_conversation_id: str, task_id: str, instruction: str) -> str:
        _ = main_conversation_id
        return f"steered @{task_id}: {instruction}"

    async def cancel_task(self, *, main_conversation_id: str, task_id: str) -> str:
        _ = main_conversation_id
        return f"cancelled @{task_id}"

    async def list_tasks(self, main_conversation_id: str) -> Sequence[TaskSummary]:
        _ = main_conversation_id
        return list(self.items)

    async def inspect_workspace_locks(
        self, work_path: str | None = None
    ) -> Sequence[WorkspaceLockStatus]:
        _ = work_path
        return []


class TestSuperAgentRouter(unittest.IsolatedAsyncioTestCase):
    """验证 Router 前台即时应答、工具调用与后台事件回流。"""

    @asynccontextmanager
    async def _env(
        self,
    ) -> AsyncGenerator[tuple[SQLModelConversationStore, ContextTreeManager, _FakeDispatcher, Path]]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = create_sqlite_async_engine(tmp_path / "router.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            tree = ContextTreeManager(store)
            _ = await tree.bootstrap()
            try:
                yield store, tree, _FakeDispatcher(), tmp_path
            finally:
                await engine.dispose()

    async def test_first_message_creates_conversation_and_persists(self) -> None:
        async with self._env() as (store, tree, dispatcher, _):
            router = SuperAgentRouter(
                model=TestModel(custom_output_text="你好，请问有什么需要帮忙？", call_tools=[]),
                context_tree=tree,
                conversation_store=store,
                task_dispatcher=dispatcher,
            )
            reply = await router.handle_user_message("你好")
            self.assertEqual(reply.output, "你好，请问有什么需要帮忙？")

            conv = await store.load(reply.conversation_id, scope="main")
            assert conv is not None
            self.assertGreaterEqual(len(conv.messages), 2)
            self.assertIsNone(await tree.get_work_path(reply.conversation_id))

    async def test_router_set_workspace_and_dispatch_nonblocking(self) -> None:
        async with self._env() as (store, tree, dispatcher, tmp_path):
            repo = tmp_path / "api_repo"
            repo.mkdir()
            _ = (repo / "AGENTS.md").write_text("API 仓库规范", encoding="utf-8")

            def _model_fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                last = messages[-1]
                if any(part.part_kind == "tool-return" for part in last.parts):
                    return ModelResponse(parts=[TextPart(content="已绑定工作区并派发 @T1")])
                return ModelResponse(
                    parts=[
                        ToolCallPart(tool_name="set_workspace", args={"path": str(repo)}, tool_call_id="c1"),
                        ToolCallPart(
                            tool_name="dispatch_role_agent",
                            args={"role_name": "coder", "mandate": "实现登录限流"},
                            tool_call_id="c2",
                        ),
                    ]
                )

            router = SuperAgentRouter(
                model=FunctionModel(_model_fn),
                context_tree=tree,
                conversation_store=store,
                task_dispatcher=dispatcher,
            )
            reply = await router.handle_user_message(f"请处理 {repo} 的登录限流")
            self.assertIn("@T1", reply.output)
            self.assertEqual(await tree.get_work_path(reply.conversation_id), str(repo.resolve()))
            self.assertEqual(len(dispatcher.items), 1)
            self.assertEqual(dispatcher.items[0].role_name, "coder")

            # 第二轮请求：验证工作区 AGENTS.md 动态注入到了模型请求末尾，但未写入持久化历史
            seen_ws_in_request: list[bool] = []

            def _second_turn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                joined = "\n".join(
                    p.content
                    for m in messages
                    for p in m.parts
                    if isinstance(p, (SystemPromptPart, UserPromptPart, TextPart)) and isinstance(p.content, str)
                )
                seen_ws_in_request.append("API 仓库规范" in joined)
                return ModelResponse(parts=[TextPart(content="收到")])

            router2 = SuperAgentRouter(
                model=FunctionModel(_second_turn),
                context_tree=tree,
                conversation_store=store,
                task_dispatcher=dispatcher,
            )
            _ = await router2.handle_user_message("继续", conversation_id=reply.conversation_id)
            self.assertEqual(seen_ws_in_request, [True])

            persisted = await store.load(reply.conversation_id, scope="main")
            assert persisted is not None
            persisted_text = "\n".join(
                p.content
                for m in persisted.messages
                for p in m.parts
                if isinstance(p, (SystemPromptPart, UserPromptPart, TextPart)) and isinstance(p.content, str)
            )
            self.assertNotIn("API 仓库规范", persisted_text)

    async def test_proactive_wakeup_when_idle_and_enqueue_when_running(self) -> None:
        async with self._env() as (store, tree, dispatcher, _):
            proactive: list[tuple[str, str]] = []

            async def _on_proactive(conv_id: str, text: str) -> None:
                proactive.append((conv_id, text))

            router = SuperAgentRouter(
                model=TestModel(custom_output_text="后台任务已完成播报", call_tools=[]),
                context_tree=tree,
                conversation_store=store,
                task_dispatcher=dispatcher,
                on_proactive_reply=_on_proactive,
            )
            reply = await router.handle_user_message("开始")
            conv_id = reply.conversation_id

            # 1. 空闲态收到 completed 事件 -> 触发主动汇报并落盘
            out = await router.on_ticket_event(conv_id, "@T1 已完成限流改造", kind="completed")
            self.assertEqual(out, "后台任务已完成播报")
            self.assertEqual(proactive, [(conv_id, "后台任务已完成播报")])

            # 2. 运行态收到事件 -> 不抢占，推入待注入队列供当前轮读取
            router._running_conversations.add(conv_id)  # pyright: ignore[reportPrivateUsage]
            try:
                queued_out = await router.on_ticket_event(conv_id, "@T2 完成了", kind="completed")
                self.assertIsNone(queued_out)
                self.assertEqual(len(router._pending_events[conv_id]), 1)  # pyright: ignore[reportPrivateUsage]
            finally:
                router._running_conversations.discard(conv_id)  # pyright: ignore[reportPrivateUsage]


if __name__ == "__main__":
    _ = unittest.main()
