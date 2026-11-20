"""Step 4.3 单元测试: TaskSteerHook、Steer/Revise、上游失败/取消依赖语义与重启恢复。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import override

from pydantic_ai import ModelRetry
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
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    RunRecord,
    SqliteStepStore,
)

from agent.context import ContextTreeManager
from agent.core import (
    RoleRegistry,
    RoleSessionManager,
    RoleSpec,
    WorkspaceLockManager,
    open_runtime,
)
from agent.db import (
    SQLModelConversationStore,
    SQLModelTicketStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)


class _SteerableCapability(AbstractCapability[object]):
    def __init__(
        self,
        *,
        step1_started: asyncio.Event | None = None,
        step1_release: asyncio.Event | None = None,
    ) -> None:
        self._toolset: FunctionToolset[object] = FunctionToolset()

        @self._toolset.tool
        async def do_work(ctx: RunContext[object], mode: str) -> str:
            _ = ctx
            if mode == "fail_hard":
                raise ModelRetry("故意触发重试上限失败")
            if step1_started is not None:
                step1_started.set()
            if step1_release is not None:
                _ = await step1_release.wait()
            return f"work_ok:{mode}"

        _ = do_work

    @override
    def get_toolset(self) -> AbstractToolset[object]:
        return self._toolset


def _make_steerable_model() -> FunctionModel:
    def responder(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = info
        all_prompts = "\n".join(
            p.content
            for msg in messages
            if isinstance(msg, ModelRequest)
            for p in msg.parts
            if isinstance(p, UserPromptPart) and isinstance(p.content, str)
        )
        has_return = any(
            isinstance(msg, ModelRequest)
            and any(isinstance(p, ToolReturnPart) for p in msg.parts)
            for msg in messages
        )
        if "STEERED_TARGET" in all_prompts:
            return ModelResponse(parts=[TextPart(content="已按 Steer 转向: STEERED_TARGET")])
        if "RETRY_SUCCESS" in all_prompts:
            return ModelResponse(parts=[TextPart(content="重试成功: RETRY_SUCCESS")])
        if "FORCE_FAIL" in all_prompts:
            return ModelResponse(parts=[ToolCallPart(tool_name="do_work", args={"mode": "fail_hard"})])
        if not has_return:
            return ModelResponse(parts=[ToolCallPart(tool_name="do_work", args={"mode": "normal"})])
        return ModelResponse(parts=[TextPart(content="正常完成")])

    return FunctionModel(responder)


class TestStep43SteerAndRecovery(unittest.IsolatedAsyncioTestCase):
    """验证 Steer 实时注入、上游失败阻断/取消解除、Revise 原链续跑与重启恢复。"""

    async def test_running_steer_and_queued_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()

            engine = create_sqlite_async_engine(tmp_path / "steer.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            _ = await conv_store.ensure_root_node()
            main = await conv_store.create_conversation_from_root(target_title="Steer主线")

            started = asyncio.Event()
            release = asyncio.Event()
            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="coder",
                    description="coder",
                    instructions="coder",
                    model=_make_steerable_model(),
                    capabilities=(_SteerableCapability(step1_started=started, step1_release=release),),
                    default_lock_mode="exclusive",
                )
            )
            scheduler = WorkspaceLockManager(
                roles=registry,
                sessions=RoleSessionManager(conversation_store=conv_store, ticket_index=ticket_store),
                ticket_store=ticket_store,
            )

            try:
                _ = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="初始任务",
                    work_path=str(repo),
                )
                _ = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="后续排队任务",
                    work_path=str(repo),
                )
                _ = await asyncio.wait_for(started.wait(), timeout=2.0)

                msg1 = await scheduler.steer_or_revise(
                    main_conversation_id=main.conversation_id,
                    task_id="T1",
                    instruction="请输出 STEERED_TARGET",
                )
                self.assertIn("实时纠偏", msg1)

                msg2 = await scheduler.steer_or_revise(
                    main_conversation_id=main.conversation_id,
                    task_id="T2",
                    instruction="顺便检查注释",
                )
                self.assertIn("合并", msg2)

                release.set()
                await asyncio.wait_for(scheduler.wait_all(), timeout=3.0)

                t1 = await ticket_store.get_ticket(main.conversation_id, "T1")
                t2 = await ticket_store.get_ticket(main.conversation_id, "T2")
                assert t1 is not None and t2 is not None
                self.assertEqual(t1.status, "completed")
                self.assertIn("STEERED_TARGET", t1.result_summary or "")
                self.assertEqual(t2.status, "completed")
                self.assertIn("[追加要求]", t2.mandate)
            finally:
                release.set()
                await scheduler.wait_all()
                await engine.dispose()

    async def test_failed_upstream_blocks_downstream_until_revised_or_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()

            engine = create_sqlite_async_engine(tmp_path / "deps.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            _ = await conv_store.ensure_root_node()
            main = await conv_store.create_conversation_from_root(target_title="依赖阻断主线")

            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="coder",
                    description="coder",
                    instructions="coder",
                    model=_make_steerable_model(),
                    capabilities=(_SteerableCapability(),),
                    default_lock_mode="exclusive",
                )
            )
            scheduler = WorkspaceLockManager(
                roles=registry,
                sessions=RoleSessionManager(conversation_store=conv_store, ticket_index=ticket_store),
                ticket_store=ticket_store,
            )

            try:
                # 1) 上游 T1 失败 -> 依赖 T1 的 T2 必须保持 queued（不放行、不自动重试）
                _ = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="FORCE_FAIL",
                    work_path=str(repo),
                )
                _ = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="下游验证",
                    work_path=str(repo),
                    depends_on=["T1"],
                )
                await asyncio.wait_for(scheduler.wait_all(), timeout=5.0)

                t1 = await ticket_store.get_ticket(main.conversation_id, "T1")
                t2 = await ticket_store.get_ticket(main.conversation_id, "T2")
                assert t1 is not None and t2 is not None
                self.assertEqual(t1.status, "failed")
                self.assertEqual(t2.status, "queued")

                # 2) 用户对上游 T1 Revise 重试成功 -> T2 自动解除阻塞并完成
                _ = await scheduler.steer_or_revise(
                    main_conversation_id=main.conversation_id,
                    task_id="T1",
                    instruction="RETRY_SUCCESS",
                )
                await asyncio.wait_for(scheduler.wait_all(), timeout=5.0)

                t1_after = await ticket_store.get_ticket(main.conversation_id, "T1")
                t2_after = await ticket_store.get_ticket(main.conversation_id, "T2")
                assert t1_after is not None and t2_after is not None
                self.assertEqual(t1_after.status, "completed")
                self.assertEqual(t2_after.status, "completed")

                # 3) 上游 T3 失败 -> 下游 T4 保持 queued -> 取消上游 T3 -> 自动解除依赖并执行 T4
                _ = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="FORCE_FAIL",
                    work_path=str(repo),
                )
                _ = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="取消上游后继续",
                    work_path=str(repo),
                    depends_on=["T3"],
                )
                await asyncio.wait_for(scheduler.wait_all(), timeout=5.0)

                t3 = await ticket_store.get_ticket(main.conversation_id, "T3")
                t4 = await ticket_store.get_ticket(main.conversation_id, "T4")
                assert t3 is not None and t4 is not None
                self.assertEqual(t3.status, "failed")
                self.assertEqual(t4.status, "queued")

                _ = await scheduler.cancel_task(
                    main_conversation_id=main.conversation_id,
                    task_id="T3",
                )
                await asyncio.wait_for(scheduler.wait_all(), timeout=5.0)

                t3_done = await ticket_store.get_ticket(main.conversation_id, "T3")
                t4_done = await ticket_store.get_ticket(main.conversation_id, "T4")
                assert t3_done is not None and t4_done is not None
                self.assertEqual(t3_done.status, "cancelled")
                self.assertEqual(t4_done.status, "completed")
            finally:
                await scheduler.wait_all()
                await engine.dispose()

    async def test_restart_recovery_repairs_interrupted_and_resumes_unblocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_a = tmp_path / "repo_a"
            repo_b = tmp_path / "repo_b"
            repo_a.mkdir()
            repo_b.mkdir()

            engine = create_sqlite_async_engine(tmp_path / "recover.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            step_store = SqliteStepStore(database=tmp_path / "steps.db")
            _ = await conv_store.ensure_root_node()
            main = await conv_store.create_conversation_from_root(target_title="恢复主线")

            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="coder",
                    description="coder",
                    instructions="coder",
                    model=_make_steerable_model(),
                    capabilities=(_SteerableCapability(),),
                    default_lock_mode="exclusive",
                )
            )

            try:
                # 模拟崩溃前的 DB 状态：T1 running（无完整结果）、T2 queued 依赖 T1、T3 queued 独立目录
                _ = await ticket_store.create_ticket(
                    main_conversation_id=main.conversation_id,
                    task_id="T1",
                    role_name="coder",
                    mandate="被中断任务",
                    work_path=str(repo_a.resolve()),
                    status="running",
                )
                _ = await ticket_store.create_ticket(
                    main_conversation_id=main.conversation_id,
                    task_id="T2",
                    role_name="coder",
                    mandate="依赖T1的排队任务",
                    work_path=str(repo_a.resolve()),
                    depends_on=["T1"],
                    status="queued",
                )
                _ = await ticket_store.create_ticket(
                    main_conversation_id=main.conversation_id,
                    task_id="T3",
                    role_name="coder",
                    mandate="独立目录任务",
                    work_path=str(repo_b.resolve()),
                    status="queued",
                )

                restarted = WorkspaceLockManager(
                    roles=registry,
                    sessions=RoleSessionManager(conversation_store=conv_store, ticket_index=ticket_store),
                    ticket_store=ticket_store,
                    step_store=step_store,
                )
                interrupted, requeued = await restarted.recover_pending_tasks(
                    main.conversation_id,
                    conversation_store=conv_store,
                )
                self.assertEqual(interrupted, ["T1"])
                self.assertEqual(requeued, ["T3"])

                await asyncio.wait_for(restarted.wait_all(), timeout=5.0)
                t1 = await ticket_store.get_ticket(main.conversation_id, "T1")
                t2 = await ticket_store.get_ticket(main.conversation_id, "T2")
                t3 = await ticket_store.get_ticket(main.conversation_id, "T3")
                assert t1 is not None and t2 is not None and t3 is not None
                self.assertEqual(t1.status, "interrupted")
                self.assertEqual(t2.status, "queued")
                self.assertEqual(t3.status, "completed")

                # 恢复执行被中断的 T1 -> 完成后 T2 自动放行完成
                _ = await restarted.steer_or_revise(
                    main_conversation_id=main.conversation_id,
                    task_id="T1",
                    instruction="RETRY_SUCCESS",
                )
                await asyncio.wait_for(restarted.wait_all(), timeout=5.0)
                t1_done = await ticket_store.get_ticket(main.conversation_id, "T1")
                t2_done = await ticket_store.get_ticket(main.conversation_id, "T2")
                assert t1_done is not None and t2_done is not None
                self.assertEqual(t1_done.status, "completed")
                self.assertEqual(t2_done.status, "completed")
            finally:
                await engine.dispose()

    async def test_main_conversation_frontier_repair_and_open_runtime_recovery_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "app.db"
            step_path = tmp_path / "steps.db"

            engine = create_sqlite_async_engine(db_path)
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            step_store = SqliteStepStore(database=step_path)

            tree = ContextTreeManager(conv_store, step_store=step_store)
            _ = await tree.bootstrap()
            main_conv = await tree.ensure_active_conversation()
            cid = main_conv.conversation_id

            # 写入一个含悬空工具调用的未完成 frontier 快照，并创建一个 running ticket
            await step_store.register_run(
                RunRecord(
                    run_id="run-main-crash",
                    conversation_id=cid,
                    agent_name="router",
                )
            )
            dangling_msgs: list[ModelMessage] = [
                ModelRequest(parts=[UserPromptPart(content="执行某操作")]),
                ModelResponse(parts=[ToolCallPart(tool_name="list_tasks", args={}, tool_call_id="tc-1")]),
            ]
            await step_store.save_snapshot(
                ContinuableSnapshot(
                    run_id="run-main-crash",
                    step_index=1,
                    messages=dangling_msgs,
                    conversation_id=cid,
                    agent_name="router",
                    state="interrupted",
                )
            )
            _ = await ticket_store.create_ticket(
                main_conversation_id=cid,
                task_id="T1",
                role_name="coder",
                mandate="中断中的后台任务",
                status="running",
            )
            await engine.dispose()

            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="coder",
                    description="coder",
                    instructions="coder",
                    model=_make_steerable_model(),
                    capabilities=(_SteerableCapability(),),
                    default_lock_mode="exclusive",
                )
            )
            async with open_runtime(
                db_path=db_path,
                step_db_path=step_path,
                role_registry=registry,
            ) as runtime:
                self.assertIn(cid, runtime.recovery_summary)
                interrupted, _ = runtime.recovery_summary[cid]
                self.assertEqual(interrupted, ["T1"])

                repaired_conv = await runtime.context_tree.load_conversation(cid)
                tool_returns = [
                    p
                    for msg in repaired_conv.messages
                    if isinstance(msg, ModelRequest)
                    for p in msg.parts
                    if isinstance(p, ToolReturnPart)
                ]
                self.assertTrue(len(tool_returns) >= 1)


if __name__ == "__main__":
    _ = unittest.main()
