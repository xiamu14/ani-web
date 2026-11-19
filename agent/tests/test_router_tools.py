"""Router 工具集与解耦协议单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from agent.context import ContextTreeManager
from agent.db import (
    SQLModelConversationStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)
from agent.tools import (
    DispatchReceipt,
    LockMode,
    RouterDeps,
    TaskSummary,
    WorkspaceLockStatus,
    build_router_toolset,
)
from agent.tools.router_tools import (
    dispatch_role_agent,
    list_tasks,
    set_workspace,
    steer_or_revise_task,
)


class _FakeTaskDispatcher:
    """用于 Step 2 独立验收的 Fake TaskDispatchPort。"""

    def __init__(self) -> None:
        self.items: list[TaskSummary] = []
        self.steered: list[tuple[str, str]] = []

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
        _ = (main_conversation_id, depends_on)
        effective_lock: LockMode = lock_mode or "exclusive"
        task_id = f"T{len(self.items) + 1}"
        busy = any(t.status == "running" and t.work_path == work_path for t in self.items)
        status = (
            "queued" if (work_path is not None and effective_lock == "exclusive" and busy) else "running"
        )
        pos = (
            sum(1 for t in self.items if t.status == "queued" and t.work_path == work_path) + 1
            if status == "queued"
            else 0
        )
        self.items.append(
            TaskSummary(
                task_id=task_id,
                role_name=role_name,
                status=status,
                work_path=work_path,
                mandate=mandate,
            )
        )
        return DispatchReceipt(task_id=task_id, status=status, queue_position=pos, work_path=work_path)

    async def steer_or_revise(
        self, *, main_conversation_id: str, task_id: str, instruction: str
    ) -> str:
        _ = main_conversation_id
        if not any(t.task_id == task_id for t in self.items):
            raise LookupError(f"任务 @{task_id} 不存在")
        self.steered.append((task_id, instruction))
        return f"已向 @{task_id} 注入指令: {instruction}"

    async def cancel_task(self, *, main_conversation_id: str, task_id: str) -> str:
        _ = main_conversation_id
        if not any(t.task_id == task_id for t in self.items):
            raise LookupError(f"任务 @{task_id} 不存在")
        return f"已取消 @{task_id}"

    async def list_tasks(self, main_conversation_id: str) -> Sequence[TaskSummary]:
        _ = main_conversation_id
        return list(self.items)

    async def inspect_workspace_locks(
        self, work_path: str | None = None
    ) -> Sequence[WorkspaceLockStatus]:
        _ = work_path
        return []


def _fake_ctx(deps: RouterDeps) -> RunContext[RouterDeps]:
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


class TestRouterTools(unittest.IsolatedAsyncioTestCase):
    """验证 Router 工具集的目录校验、派发回执、三级消歧守卫与 TestModel 集成。"""

    @asynccontextmanager
    async def _env(self) -> AsyncGenerator[tuple[ContextTreeManager, _FakeTaskDispatcher, Path]]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = create_sqlite_async_engine(tmp_path / "router_tools.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            manager = ContextTreeManager(store)
            _ = await manager.bootstrap()
            try:
                yield manager, _FakeTaskDispatcher(), tmp_path
            finally:
                await engine.dispose()

    async def test_set_workspace_and_invalid_path_retry(self) -> None:
        async with self._env() as (manager, fake_tasks, tmp_path):
            repo = tmp_path / "repo"
            repo.mkdir()
            _ = (repo / "AGENTS.md").write_text("测试工作区规则", encoding="utf-8")

            conv = await manager.ensure_active_conversation()
            ctx = _fake_ctx(RouterDeps(main_conversation_id=conv.conversation_id, workspace=manager, tasks=fake_tasks))

            msg = await set_workspace(ctx, str(repo))
            self.assertIn(str(repo.resolve()), msg)
            self.assertIn("已加载该目录 AGENTS.md", msg)

            with self.assertRaises(ModelRetry):
                _ = await set_workspace(ctx, str(tmp_path / "not_exists"))

    async def test_dispatch_and_steer_confirmation_guard(self) -> None:
        async with self._env() as (manager, fake_tasks, tmp_path):
            repo = tmp_path / "repo_a"
            repo.mkdir()
            conv = await manager.ensure_active_conversation(work_path=repo)
            ctx = _fake_ctx(RouterDeps(main_conversation_id=conv.conversation_id, workspace=manager, tasks=fake_tasks))

            r1 = await dispatch_role_agent(ctx, "coder", "重构登录")
            self.assertIn("@T1", r1)
            self.assertIn("已在后台启动", r1)

            r2 = await dispatch_role_agent(ctx, "tester", "运行单测")
            self.assertIn("@T2", r2)
            self.assertIn("第 1 位", r2)

            # 未确认时返回 CONFIRM_NEEDED，绝不直接调用 steer_or_revise
            confirm_msg = await steer_or_revise_task(ctx, "@T1", "改用令牌桶", user_confirmed=False)
            self.assertTrue(confirm_msg.startswith("CONFIRM_NEEDED:"))
            self.assertEqual(fake_tasks.steered, [])

            # 确认后才真正执行
            steered_msg = await steer_or_revise_task(ctx, "@T1", "改用令牌桶", user_confirmed=True)
            self.assertIn("@T1", steered_msg)
            self.assertEqual(fake_tasks.steered, [("T1", "改用令牌桶")])

            listing = await list_tasks(ctx)
            self.assertIn("@T1 [coder]", listing)
            self.assertIn("@T2 [tester]", listing)

    async def test_agent_integration_with_test_model(self) -> None:
        async with self._env() as (manager, fake_tasks, tmp_path):
            repo = tmp_path / "repo_c"
            repo.mkdir()
            conv = await manager.ensure_active_conversation()
            deps = RouterDeps(main_conversation_id=conv.conversation_id, workspace=manager, tasks=fake_tasks)

            agent = Agent(
                model=TestModel(call_tools=["list_tasks"]),
                deps_type=RouterDeps,
                toolsets=[build_router_toolset()],
            )
            result = await agent.run("查看任务", deps=deps)
            self.assertIsInstance(cast(object, result.output), str)


if __name__ == "__main__":
    _ = unittest.main()
