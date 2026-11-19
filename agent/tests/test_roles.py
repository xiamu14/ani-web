"""Step 4.1 单元测试: RoleSpec、RoleRegistry 与 RoleSessionManager。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic_ai.models.test import TestModel
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import Coder, FileSystem, Shell
from pydantic_ai_harness.step_persistence import SqliteStepStore

from agent.core import (
    RoleRegistry,
    RoleSessionManager,
    RoleSpec,
    build_default_role_registry,
)
from agent.db import (
    SQLModelConversationStore,
    SQLModelTicketStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)


class TestRolesAndSessionManager(unittest.IsolatedAsyncioTestCase):
    """验证 Harness Role 能力装配与任务级 Context Window 隔离持久化。"""

    @asynccontextmanager
    async def _env(
        self,
    ) -> AsyncGenerator[tuple[SQLModelConversationStore, SQLModelTicketStore, RoleSessionManager, SqliteStepStore, Path]]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = create_sqlite_async_engine(tmp_path / "roles.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            conv_store = SQLModelConversationStore(engine, session_factory=sessions)
            ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
            manager = RoleSessionManager(conversation_store=conv_store, ticket_index=ticket_store)
            step_conn = sqlite3.connect(":memory:", check_same_thread=False)
            step_store = SqliteStepStore(connection=step_conn)
            try:
                yield conv_store, ticket_store, manager, step_store, tmp_path
            finally:
                await engine.dispose()
                step_conn.close()

    def test_default_role_registry_uses_harness_capabilities(self) -> None:
        registry: RoleRegistry = build_default_role_registry(TestModel(call_tools=[]))
        names = {spec.name for spec in registry.list_roles()}
        self.assertEqual(names, {"coder", "reviewer", "tester", "researcher"})

        self.assertTrue(any(isinstance(c, Coder) for c in registry.get("coder").capabilities))
        self.assertTrue(any(isinstance(c, FileSystem) and c.read_only for c in registry.get("reviewer").capabilities))
        tester_caps = registry.get("tester").capabilities
        self.assertTrue(any(isinstance(c, FileSystem) for c in tester_caps))
        self.assertTrue(any(isinstance(c, Shell) for c in tester_caps))
        self.assertTrue(any(isinstance(c, FileSystem) and c.read_only for c in registry.get("researcher").capabilities))
        self.assertEqual(registry.get("researcher").default_lock_mode, "shared")
        self.assertEqual(registry.get("reviewer").default_lock_mode, "shared")

        with self.assertRaises(LookupError):
            _ = registry.get("nonexistent")

    async def test_task_context_isolation_and_revise_continuity(self) -> None:
        async with self._env() as (conv_store, ticket_store, manager, step_store, tmp_path):
            _ = await conv_store.ensure_root_node()
            c1 = await conv_store.create_conversation_from_root(target_title="主分支1")
            c2 = await conv_store.create_conversation_from_root(target_title="主分支2")
            assert c1.head_id is not None and c2.head_id is not None

            _ = await ticket_store.create_ticket(
                main_conversation_id=c1.conversation_id,
                task_id="T1",
                role_name="coder",
                mandate="实现功能 A",
                anchor_entry_id=c1.head_id,
            )
            _ = await ticket_store.create_ticket(
                main_conversation_id=c1.conversation_id,
                task_id="T2",
                role_name="coder",
                mandate="实现功能 B",
                anchor_entry_id=c1.head_id,
            )
            _ = await ticket_store.create_ticket(
                main_conversation_id=c2.conversation_id,
                task_id="T1",
                role_name="coder",
                mandate="主分支2的 T1",
                anchor_entry_id=c2.head_id,
            )

            spec = RoleSpec(
                name="coder",
                description="test coder",
                instructions="保持简洁",
                model=TestModel(custom_output_text="coder-done", call_tools=[]),
            )
            agent = spec.build_agent(step_store=step_store)

            # 1. 运行 C1 的 T1，保存后再次加载应保留历史（支持 Revise）
            conv_c1_t1 = await manager.load_task_conversation(
                main_conversation_id=c1.conversation_id,
                task_id="T1",
                role_name="coder",
                role_instructions=spec.instructions,
            )
            res1 = await agent.run(
                "实现功能 A",
                conversation=conv_c1_t1,
                workspace=LocalWorkspaceBackend(tmp_path),
            )
            await manager.save_task_result(c1.conversation_id, res1)

            # 2. 同分支同角色 T2 与 另一主分支 C2 的 T1 各自拥有独立 conversation_id
            conv_c1_t2 = await manager.load_task_conversation(
                main_conversation_id=c1.conversation_id,
                task_id="T2",
                role_name="coder",
                role_instructions=spec.instructions,
            )
            conv_c2_t1 = await manager.load_task_conversation(
                main_conversation_id=c2.conversation_id,
                task_id="T1",
                role_name="coder",
                role_instructions=spec.instructions,
            )
            self.assertEqual(
                len({conv_c1_t1.conversation_id, conv_c1_t2.conversation_id, conv_c2_t1.conversation_id}),
                3,
            )

            # 3. Revise 再次加载 C1 的 T1，保留上一轮问答上下文
            reloaded_c1_t1 = await manager.load_task_conversation(
                main_conversation_id=c1.conversation_id,
                task_id="T1",
                role_name="coder",
                role_instructions=spec.instructions,
            )
            self.assertGreater(len(reloaded_c1_t1.messages), len(conv_c1_t2.messages))


if __name__ == "__main__":
    _ = unittest.main()
