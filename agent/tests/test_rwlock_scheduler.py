"""Step 4.2 补充单测: 读写锁（shared/exclusive）、防写者饥饿、父子目录重叠与 depends_on。"""

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
from pydantic_ai_harness.step_persistence import SqliteStepStore

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


class _TagGateCapability(AbstractCapability[object]):
    """按任务标签控制启动信号与放行闸门的测试 Capability。"""

    def __init__(
        self,
        *,
        started: dict[str, asyncio.Event],
        releases: dict[str, asyncio.Event],
        events: list[str],
    ) -> None:
        self._toolset: FunctionToolset[object] = FunctionToolset()

        @self._toolset.tool
        async def read_file(ctx: RunContext[object], path: str) -> str:
            _ = ctx
            events.append(f"start:{path}")
            if path in started:
                started[path].set()
            if path in releases:
                _ = await releases[path].wait()
            events.append(f"end:{path}")
            return f"ok:{path}"

        _ = read_file

    @override
    def get_toolset(self) -> AbstractToolset[object]:
        return self._toolset


def _make_tag_model() -> FunctionModel:
    def responder(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = info
        has_return = any(
            isinstance(msg, ModelRequest)
            and any(isinstance(p, ToolReturnPart) for p in msg.parts)
            for msg in messages
        )
        if not has_return:
            prompt = " ".join(
                p.content
                for msg in messages
                if isinstance(msg, ModelRequest)
                for p in msg.parts
                if isinstance(p, UserPromptPart) and isinstance(p.content, str)
            )
            tag = prompt.split("|", 1)[0].strip()
            return ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": tag})])
        return ModelResponse(parts=[TextPart(content="done")])

    return FunctionModel(responder)


class TestRWLockScheduler(unittest.IsolatedAsyncioTestCase):
    """验证 shared 并发、exclusive 互斥、防写者饥饿、父子目录重叠及 depends_on。"""

    async def test_shared_readers_concurrent_and_anti_writer_starvation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            sub_src = repo / "src"
            sub_src.mkdir(parents=True)

            engine = create_sqlite_async_engine(tmp_path / "rw.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            _ = await conv_store.ensure_root_node()
            main = await conv_store.create_conversation_from_root(target_title="RW主线")

            started = {k: asyncio.Event() for k in ("r1", "rev1", "w1", "r2", "rev2")}
            releases = {k: asyncio.Event() for k in ("r1", "rev1", "w1", "r2", "rev2")}
            exec_log: list[str] = []

            cap = _TagGateCapability(started=started, releases=releases, events=exec_log)
            model = _make_tag_model()
            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="researcher",
                    description="read",
                    instructions="read",
                    model=model,
                    capabilities=(cap,),
                    default_lock_mode="shared",
                )
            )
            registry.register(
                RoleSpec(
                    name="reviewer",
                    description="review",
                    instructions="review",
                    model=model,
                    capabilities=(cap,),
                    default_lock_mode="shared",
                )
            )
            registry.register(
                RoleSpec(
                    name="coder",
                    description="write",
                    instructions="write",
                    model=model,
                    capabilities=(cap,),
                    default_lock_mode="exclusive",
                )
            )

            scheduler = WorkspaceLockManager(
                roles=registry,
                sessions=RoleSessionManager(conversation_store=conv_store, ticket_index=ticket_store),
                ticket_store=ticket_store,
            )

            try:
                # 1) 同目录的两个只读任务（researcher + reviewer）应同时并发运行
                t_r1 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="researcher",
                    mandate="r1|调研",
                    work_path=str(repo),
                )
                t_rev1 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="reviewer",
                    mandate="rev1|审查",
                    work_path=str(repo),
                )
                self.assertEqual(t_r1.status, "running")
                self.assertEqual(t_rev1.status, "running")
                _ = await asyncio.wait_for(started["r1"].wait(), timeout=2.0)
                _ = await asyncio.wait_for(started["rev1"].wait(), timeout=2.0)

                # 2) coder 在子目录 repo/src 申请写锁：父子目录重叠且有活跃读锁 -> 必须排队
                t_w1 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="coder",
                    mandate="w1|修改源码",
                    work_path=str(sub_src),
                    lock_mode="none",  # 验证写角色不能被降级
                )
                self.assertEqual(t_w1.status, "queued")

                # 3) coder 排队期间新来的只读任务不能插队（防止写者饥饿）
                t_r2 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="researcher",
                    mandate="r2|复查",
                    work_path=str(repo),
                )
                t_rev2 = await scheduler.dispatch_task(
                    main_conversation_id=main.conversation_id,
                    role_name="reviewer",
                    mandate="rev2|复审",
                    work_path=str(repo),
                )
                self.assertEqual(t_r2.status, "queued")
                self.assertEqual(t_rev2.status, "queued")

                locks = await scheduler.inspect_workspace_locks(str(repo))
                self.assertTrue(any("@T1" in s.shared_holders and "@T2" in s.shared_holders for s in locks))

                # 4) 放行初始读锁 -> w1 独占启动，r2/rev2 仍需等 w1 完成
                releases["r1"].set()
                releases["rev1"].set()
                _ = await asyncio.wait_for(started["w1"].wait(), timeout=2.0)
                self.assertFalse(started["r2"].is_set())
                self.assertFalse(started["rev2"].is_set())

                # 5) 放行 w1 -> 队首连续的两个读任务 r2 与 rev2 应同时唤醒并发
                releases["w1"].set()
                _ = await asyncio.wait_for(started["r2"].wait(), timeout=2.0)
                _ = await asyncio.wait_for(started["rev2"].wait(), timeout=2.0)

                releases["r2"].set()
                releases["rev2"].set()
                await asyncio.wait_for(scheduler.wait_all(), timeout=3.0)

                self.assertLess(exec_log.index("end:r1"), exec_log.index("start:w1"))
                self.assertLess(exec_log.index("end:rev1"), exec_log.index("start:w1"))
                self.assertLess(exec_log.index("end:w1"), exec_log.index("start:r2"))
                self.assertLess(exec_log.index("end:w1"), exec_log.index("start:rev2"))
            finally:
                for ev in releases.values():
                    ev.set()
                await scheduler.wait_all()
                await engine.dispose()

    async def test_relative_subdir_resolution_and_sibling_concurrency_with_parent_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            pkg_a = repo / "pkg_a"
            pkg_b = repo / "pkg_b"
            pkg_a.mkdir(parents=True)
            pkg_b.mkdir(parents=True)

            engine = create_sqlite_async_engine(tmp_path / "nested.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            tree = ContextTreeManager(conv_store)
            _ = await tree.bootstrap()

            started = {k: asyncio.Event() for k in ("a1", "b1", "root1")}
            releases = {k: asyncio.Event() for k in ("a1", "b1", "root1")}
            exec_log: list[str] = []

            cap = _TagGateCapability(started=started, releases=releases, events=exec_log)
            role_model = _make_tag_model()
            registry = RoleRegistry()
            registry.register(
                RoleSpec(
                    name="coder",
                    description="write",
                    instructions="write",
                    model=role_model,
                    capabilities=(cap,),
                    default_lock_mode="exclusive",
                )
            )
            scheduler = WorkspaceLockManager(
                roles=registry,
                sessions=RoleSessionManager(conversation_store=conv_store, ticket_index=ticket_store),
                ticket_store=ticket_store,
            )

            def router_responder(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = info
                tool_returns = [
                    p
                    for msg in messages
                    if isinstance(msg, ModelRequest)
                    for p in msg.parts
                    if isinstance(p, ToolReturnPart)
                ]
                has_dispatch_prompt = any(
                    isinstance(msg, ModelRequest)
                    and any(
                        isinstance(p, UserPromptPart)
                        and isinstance(p.content, str)
                        and "派发兄弟子目录" in p.content
                        for p in msg.parts
                    )
                    for msg in messages
                )
                if has_dispatch_prompt and len(tool_returns) == 0:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(tool_name="set_workspace", args={"path": str(repo)}),
                        ]
                    )
                if has_dispatch_prompt and len(tool_returns) == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="dispatch_role_agent",
                                args={"role_name": "coder", "mandate": "a1|修改 pkg_a", "work_path": "pkg_a"},
                            ),
                            ToolCallPart(
                                tool_name="dispatch_role_agent",
                                args={"role_name": "coder", "mandate": "b1|修改 pkg_b", "work_path": "pkg_b"},
                            ),
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="已派发子目录任务")])

            router = SuperAgentRouter(
                model=FunctionModel(router_responder),
                context_tree=tree,
                conversation_store=conv_store,
                task_dispatcher=scheduler,
            )

            try:
                reply = await router.handle_user_message("派发兄弟子目录相对路径任务")
                _ = await asyncio.wait_for(started["a1"].wait(), timeout=2.0)
                _ = await asyncio.wait_for(started["b1"].wait(), timeout=2.0)

                # 验证相对路径已被解析为 repo/pkg_a 与 repo/pkg_b，且父目录 repo 会等待两者释放
                root_receipt = await scheduler.dispatch_task(
                    main_conversation_id=reply.conversation_id,
                    role_name="coder",
                    mandate="root1|全仓重构",
                    work_path=str(repo),
                )
                self.assertEqual(root_receipt.status, "queued")

                overlaps = await scheduler.inspect_workspace_locks(str(pkg_a))
                self.assertTrue(any(s.work_path == str(pkg_a.resolve()) for s in overlaps))

                releases["a1"].set()
                releases["b1"].set()
                _ = await asyncio.wait_for(started["root1"].wait(), timeout=2.0)
                releases["root1"].set()
                await asyncio.wait_for(scheduler.wait_all(), timeout=3.0)

                self.assertLess(exec_log.index("end:a1"), exec_log.index("start:root1"))
                self.assertLess(exec_log.index("end:b1"), exec_log.index("start:root1"))

                # 验证 ticket_store.recover_main_conversation_tasks 也支持父子目录嵌套阻塞判定
                _ = await ticket_store.create_ticket(
                    main_conversation_id=reply.conversation_id,
                    task_id="T90",
                    role_name="coder",
                    mandate="父目录运行中",
                    work_path=str(repo.resolve()),
                    status="running",
                )
                _ = await ticket_store.create_ticket(
                    main_conversation_id=reply.conversation_id,
                    task_id="T91",
                    role_name="coder",
                    mandate="子目录排队中",
                    work_path=str(pkg_a.resolve()),
                    status="queued",
                )
                interrupted, requeue = await ticket_store.recover_main_conversation_tasks(
                    reply.conversation_id,
                    conversation_store=conv_store,
                    step_store=SqliteStepStore(database=tmp_path / "steps.db"),
                )
                self.assertIn("T90", interrupted)
                self.assertNotIn("T91", requeue)
            finally:
                for ev in releases.values():
                    ev.set()
                await scheduler.wait_all()
                await engine.dispose()


if __name__ == "__main__":
    _ = unittest.main()
