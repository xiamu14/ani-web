"""Step 4.2 单元测试: WorkspaceLockManager 与 Router 真实 Role 异步调度。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import override

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from agent.context import ContextTreeManager
from agent.core import (
    RoleRegistry,
    RoleSessionManager,
    RoleSpec,
    SuperAgentRouter,
    WorkspaceLockManager,
)
from agent.db import (
    SQLModelConversationStore,
    SQLModelTicketStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)
from agent.tools import TaskDispatchPort


class _AsyncStepCapability(AbstractCapability[object]):
    """提供多步异步工具的测试用 Capability。"""

    def __init__(
        self,
        *,
        started_event: asyncio.Event | None = None,
        release_event: asyncio.Event | None = None,
        log: list[str] | None = None,
    ) -> None:
        self._toolset: FunctionToolset[object] = FunctionToolset()
        self._started_event: asyncio.Event | None = started_event
        self._release_event: asyncio.Event | None = release_event
        self._log: list[str] = log if log is not None else []

        @self._toolset.tool_plain
        async def step_one(target: str) -> str:
            self._log.append(f"step_one:{target}")
            if self._started_event is not None:
                self._started_event.set()
            if self._release_event is not None:
                _ = await self._release_event.wait()
            return f"step_one_done:{target}"

        @self._toolset.tool_plain
        async def step_two(summary: str) -> str:
            self._log.append(f"step_two:{summary}")
            return f"step_two_done:{summary}"

        _ = step_one
        _ = step_two

    @override
    def get_toolset(self) -> AbstractToolset[object]:
        return self._toolset


def _make_two_step_role_model(role_label: str) -> FunctionModel:
    """构造一个会依次调用 step_one -> step_two -> 返回总结的 FunctionModel。"""

    def responder(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = info
        tool_returns = [
            part
            for msg in messages
            if isinstance(msg, ModelRequest)
            for part in msg.parts
            if isinstance(part, ToolReturnPart)
        ]
        if len(tool_returns) == 0:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="step_one", args={"target": role_label})]
            )
        if len(tool_returns) == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="step_two", args={"summary": role_label})]
            )
        return ModelResponse(parts=[TextPart(content=f"{role_label} 已完成两步任务")])

    return FunctionModel(responder)


def _make_router_dispatch_model() -> FunctionModel:
    """构造主对话 Router 测试模型：首轮并行派发 coder 与 researcher，二轮直接回复。"""
    turn_counter = {"count": 0}

    def responder(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = info
        tool_returns = [
            part
            for msg in messages
            if isinstance(msg, ModelRequest)
            for part in msg.parts
            if isinstance(part, ToolReturnPart)
        ]
        last_prompts = [
            part.content
            for msg in messages[-1:]
            if isinstance(msg, ModelRequest)
            for part in msg.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ]
        last_prompt = " ".join(last_prompts)
        if "继续" in last_prompt:
            return ModelResponse(parts=[TextPart(content="主对话未阻塞，随时可继续交流。")])
        if "@T" in last_prompt:
            return ModelResponse(parts=[TextPart(content=f"收到后台回流: {last_prompt}")])

        if turn_counter["count"] == 0 and len(tool_returns) == 0:
            turn_counter["count"] += 1
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="dispatch_role_agent",
                        args={
                            "role_name": "coder",
                            "mandate": "修改模块 A",
                            "lock_mode": "exclusive",
                        },
                    ),
                    ToolCallPart(
                        tool_name="dispatch_role_agent",
                        args={
                            "role_name": "researcher",
                            "mandate": "调研依赖 B",
                            "lock_mode": "none",
                        },
                    ),
                ]
            )

        return ModelResponse(
            parts=[TextPart(content="已派发 @T1 (coder) 与 @T2 (researcher) 异步执行。")]
        )

    return FunctionModel(responder)


class TestWorkspaceLockManager(unittest.IsolatedAsyncioTestCase):
    """验证 Router 唤起多 Role 异步多步任务与同目录 FIFO 排队调度。"""

    async def test_router_dispatches_multiple_roles_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = create_sqlite_async_engine(tmp_path / "app.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            tree = ContextTreeManager(conv_store)
            _ = await tree.bootstrap(global_agents_path=tmp_path / "AGENTS.md")

            coder_started = asyncio.Event()
            coder_release = asyncio.Event()
            step_log: list[str] = []

            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="coder",
                    description="代码实现专家",
                    instructions="执行两步工具调用完成代码任务。",
                    model=_make_two_step_role_model("coder"),
                    capabilities=(
                        _AsyncStepCapability(
                            started_event=coder_started,
                            release_event=coder_release,
                            log=step_log,
                        ),
                    ),
                    default_lock_mode="exclusive",
                )
            )
            registry.register(
                RoleSpec(
                    name="researcher",
                    description="只读调研专家",
                    instructions="执行两步工具调用完成调研。",
                    model=_make_two_step_role_model("researcher"),
                    capabilities=(_AsyncStepCapability(log=step_log),),
                    default_lock_mode="none",
                )
            )

            role_sessions = RoleSessionManager(
                conversation_store=conv_store, ticket_index=ticket_store
            )
            proactive_messages: list[tuple[str, str]] = []

            async def on_proactive(cid: str, text: str) -> None:
                proactive_messages.append((cid, text))

            scheduler = WorkspaceLockManager(
                roles=registry,
                sessions=role_sessions,
                ticket_store=ticket_store,
            )
            dispatcher: TaskDispatchPort = scheduler
            router = SuperAgentRouter(
                model=_make_router_dispatch_model(),
                context_tree=tree,
                conversation_store=conv_store,
                task_dispatcher=dispatcher,
                on_proactive_reply=on_proactive,
            )
            scheduler.set_event_handler(
                lambda cid, summary, kind: router.on_ticket_event(cid, summary, kind=kind)
            )

            try:
                reply1 = await router.handle_user_message("请让 coder 和 researcher 并行执行任务")
                self.assertIn("@T1", reply1.output)
                self.assertIn("@T2", reply1.output)

                # 等待 coder 进入 step_one 阻塞点，同时验证主对话不被阻塞
                _ = await asyncio.wait_for(coder_started.wait(), timeout=2.0)
                reply2 = await router.handle_user_message("继续聊聊当前状态")
                self.assertIn("主对话未阻塞", reply2.output)

                # 放行 coder，等待所有后台任务完成
                coder_release.set()
                await asyncio.wait_for(scheduler.wait_all(), timeout=3.0)

                tasks = await scheduler.list_tasks(main_conversation_id=reply1.conversation_id)
                by_id = {t.task_id: t for t in tasks}
                by_role = {t.role_name: t for t in tasks}
                self.assertEqual(set(by_id.keys()), {"T1", "T2"})
                self.assertEqual(by_role["coder"].status, "completed")
                self.assertEqual(by_role["researcher"].status, "completed")

                self.assertIn("step_one:coder", step_log)
                self.assertIn("step_two:coder", step_log)
                self.assertIn("step_one:researcher", step_log)
                self.assertIn("step_two:researcher", step_log)
                self.assertEqual(len(proactive_messages), 2)
                joined = "\n".join(msg for _, msg in proactive_messages)
                self.assertIn("@T1", joined)
                self.assertIn("@T2", joined)
            finally:
                coder_release.set()
                await scheduler.wait_all()
                await engine.dispose()

    async def test_same_directory_queues_fifo_and_different_directory_runs_concurrently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_a = tmp_path / "repo_a"
            repo_b = tmp_path / "repo_b"
            repo_a.mkdir()
            repo_b.mkdir()

            engine = create_sqlite_async_engine(tmp_path / "lock.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            _ = await conv_store.ensure_root_node()
            main = await conv_store.create_conversation_from_root(target_title="主线")

            t1_started = asyncio.Event()
            t1_release = asyncio.Event()
            t3_started = asyncio.Event()
            exec_order: list[str] = []

            class _GatedCapability(AbstractCapability[object]):
                def __init__(self) -> None:
                    self._toolset: FunctionToolset[object] = FunctionToolset()

                    @self._toolset.tool
                    async def run_job(ctx: RunContext[object], tag: str) -> str:
                        _ = ctx
                        exec_order.append(f"start:{tag}")
                        if tag == "job1":
                            t1_started.set()
                            _ = await t1_release.wait()
                        elif tag == "job3":
                            t3_started.set()
                        exec_order.append(f"end:{tag}")
                        return f"ok:{tag}"

                    _ = run_job

                @override
                def get_toolset(self) -> AbstractToolset[object]:
                    return self._toolset

            def lock_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = info
                has_return = any(
                    isinstance(msg, ModelRequest)
                    and any(isinstance(p, ToolReturnPart) for p in msg.parts)
                    for msg in messages
                )
                if not has_return:
                    prompt_text = " ".join(
                        part.content
                        for msg in messages
                        if isinstance(msg, ModelRequest)
                        for part in msg.parts
                        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
                    )
                    tag = "job1"
                    if "任务 2" in prompt_text:
                        tag = "job2"
                    elif "任务 3" in prompt_text:
                        tag = "job3"
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="run_job", args={"tag": tag})]
                    )
                return ModelResponse(parts=[TextPart(content="done")])

            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="coder",
                    description="测试并发与目录锁",
                    instructions="调用 run_job。",
                    model=FunctionModel(lock_model),
                    capabilities=(_GatedCapability(),),
                    default_lock_mode="exclusive",
                )
            )
            role_sessions = RoleSessionManager(
                conversation_store=conv_store, ticket_index=ticket_store
            )
            events: list[tuple[str, str, str]] = []

            async def capture_event(cid: str, summary: str, kind: str) -> None:
                events.append((cid, summary, kind))

            scheduler = WorkspaceLockManager(
                roles=registry,
                sessions=role_sessions,
                ticket_store=ticket_store,
                on_event=capture_event,
            )

            try:
                r1 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="任务 1",
                    work_path=str(repo_a),
                    lock_mode="exclusive",
                )
                self.assertEqual(r1.status, "running")
                _ = await asyncio.wait_for(t1_started.wait(), timeout=2.0)

                # 同目录 repo_a 必须排队
                r2 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="任务 2",
                    work_path=str(repo_a),
                    lock_mode="exclusive",
                )
                self.assertEqual(r2.status, "queued")
                self.assertEqual(r2.queue_position, 1)

                # 不同目录 repo_b 必须立即并发执行
                r3 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="任务 3",
                    work_path=str(repo_b),
                    lock_mode="exclusive",
                )
                self.assertEqual(r3.status, "running")
                _ = await asyncio.wait_for(t3_started.wait(), timeout=2.0)

                # 放行 T1，T2 应自动接管锁并完成
                t1_release.set()
                await asyncio.wait_for(scheduler.wait_all(), timeout=3.0)

                tasks = await scheduler.list_tasks(main_conversation_id=main.conversation_id)
                self.assertEqual([t.status for t in tasks], ["completed", "completed", "completed"])
                # job2 必须在 job1 结束后才开始
                self.assertLess(exec_order.index("end:job1"), exec_order.index("start:job2"))
                completed_events = [e for e in events if e[2] == "completed"]
                self.assertEqual(len(completed_events), 3)
            finally:
                t1_release.set()
                await scheduler.wait_all()
                await engine.dispose()


if __name__ == "__main__":
    _ = unittest.main()
