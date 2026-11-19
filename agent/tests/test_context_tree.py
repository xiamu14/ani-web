"""ContextTreeManager 与规则边界加载单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

from agent.context import ContextTreeManager
from agent.db import (
    SQLModelConversationStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)


class TestContextTreeManager(unittest.IsolatedAsyncioTestCase):
    """验证 Root 创世、目录边界 AGENTS.md、Handoff 分叉与重启恢复。"""

    @asynccontextmanager
    async def _env(self) -> AsyncGenerator[tuple[SQLModelConversationStore, ContextTreeManager, Path]]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "context.db"
            engine = create_sqlite_async_engine(db_path)
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            manager = ContextTreeManager(store)
            try:
                yield store, manager, tmp_path
            finally:
                await engine.dispose()

    async def test_bootstrap_first_conversation_and_restart_restore(self) -> None:
        async with self._env() as (store, manager, tmp_path):
            global_md = tmp_path / "global_AGENTS.md"
            _ = global_md.write_text("全局规则：优先中文回答。", encoding="utf-8")

            last_id = await manager.bootstrap(global_agents_path=global_md)
            self.assertIsNone(last_id)
            self.assertIsNotNone(manager.root_entry_id)

            conv = await manager.ensure_active_conversation(target_title="首次会话")
            self.assertIsNone(await manager.get_work_path(conv.conversation_id))
            self.assertEqual(len(conv.messages), 1)

            conv.messages.append(ModelRequest(parts=[UserPromptPart(content="你好")]))
            _ = await store.save(conv, scope="main")

            # 模拟重启：新建 ContextTreeManager 实例恢复上一次活跃会话
            restarted = ContextTreeManager(store)
            restored_id = await restarted.bootstrap()
            self.assertEqual(restored_id, conv.conversation_id)

            restored_conv = await restarted.ensure_active_conversation()
            self.assertEqual(restored_conv.conversation_id, conv.conversation_id)
            self.assertEqual(len(restored_conv.messages), 2)

    async def test_bind_workspace_boundary_and_invalid_path(self) -> None:
        async with self._env() as (_, manager, tmp_path):
            parent_dir = tmp_path / "mono"
            sub_repo = parent_dir / "service_a"
            empty_repo = parent_dir / "empty_service"
            sub_repo.mkdir(parents=True)
            empty_repo.mkdir(parents=True)

            _ = (parent_dir / "AGENTS.md").write_text("上层仓库规则：禁止继承", encoding="utf-8")
            _ = (sub_repo / "AGENTS.md").write_text("子目录规则：仅限 service_a", encoding="utf-8")

            conv = await manager.ensure_active_conversation()
            binding = await manager.bind_workspace(conv.conversation_id, sub_repo)
            self.assertEqual(binding.work_path, str(sub_repo.resolve()))
            self.assertEqual(binding.agents_md, "子目录规则：仅限 service_a")

            reloaded = await manager.load_conversation(conv.conversation_id)
            self.assertEqual(reloaded.messages, [])  # 工作区规则不写入持久化历史

            ws_prompt = await manager.get_active_workspace_prompt(conv.conversation_id)
            assert ws_prompt is not None
            self.assertIn("仅限 service_a", ws_prompt)
            self.assertNotIn("禁止继承", ws_prompt)

            # 同会话切换到无 AGENTS.md 的目录后，旧目录规则立即失效且不新建分支
            empty_binding = await manager.bind_workspace(conv.conversation_id, empty_repo)
            self.assertEqual(empty_binding.conversation_id, conv.conversation_id)
            self.assertIsNone(empty_binding.agents_md)
            self.assertIsNone(await manager.get_active_workspace_prompt(conv.conversation_id))

            with self.assertRaises(ValueError):
                _ = await manager.bind_workspace(conv.conversation_id, tmp_path / "missing_dir")

    async def test_switch_and_fork_handoff_isolation(self) -> None:
        async with self._env() as (store, manager, tmp_path):
            repo_b = tmp_path / "repo_b"
            repo_b.mkdir()
            _ = (repo_b / "AGENTS.md").write_text("Repo B 规范", encoding="utf-8")

            c1 = await manager.ensure_active_conversation(target_title="主分支1")
            c1.messages.append(ModelRequest(parts=[UserPromptPart(content="C1 冗长历史")]))
            _ = await store.save(c1, scope="main")

            c2 = await manager.fork_handoff(
                c1.conversation_id,
                summary="仅携带 API 契约结论",
                work_path=repo_b,
                target_title="派生分支2",
            )
            self.assertNotEqual(c2.conversation_id, c1.conversation_id)
            self.assertEqual(manager.active_conversation_id, c2.conversation_id)
            self.assertEqual(await manager.get_work_path(c2.conversation_id), str(repo_b.resolve()))

            # C2 通过 stop_backtrack=1 隔离了 C1 的冗长历史
            contents = [
                part.content
                for msg in c2.messages
                for part in msg.parts
                if isinstance(part, (SystemPromptPart, UserPromptPart)) and isinstance(part.content, str)
            ]
            joined = "\n".join(contents)
            self.assertIn("仅携带 API 契约结论", joined)
            self.assertNotIn("C1 冗长历史", joined)
            ws_b = await manager.get_active_workspace_prompt(c2.conversation_id)
            assert ws_b is not None
            self.assertIn("Repo B 规范", ws_b)

            # 切回 C1
            back_c1 = await manager.switch_conversation(c1.conversation_id)
            self.assertEqual(back_c1.conversation_id, c1.conversation_id)
            self.assertEqual(await store.get_last_active_conversation_id(), c1.conversation_id)

            with self.assertRaises(LookupError):
                _ = await manager.load_conversation("does-not-exist")


if __name__ == "__main__":
    _ = unittest.main()
