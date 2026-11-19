"""仓库层验收测试: ConversationStore + TicketStore + SqliteStepStore。"""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    RunRecord,
    SqliteStepStore,
)

from agent.db import (
    ContextEntryRow,
    ConversationRow,
    SQLModelConversationStore,
    SQLModelTicketStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)


def _user_req(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant_resp(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


class TestDbStores(unittest.IsolatedAsyncioTestCase):
    """验证 SQLModelConversationStore 与 SQLModelTicketStore 的核心行为。"""

    @asynccontextmanager
    async def _stores(
        self,
    ) -> AsyncGenerator[
        tuple[SQLModelConversationStore, SQLModelTicketStore, SqliteStepStore]
    ]:
        engine = create_sqlite_async_engine(":memory:")
        await init_async_db(engine)
        sessions = create_session_factory(engine)
        conv_store = SQLModelConversationStore(engine, session_factory=sessions)
        ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
        step_conn = sqlite3.connect(":memory:", check_same_thread=False)
        step_store = SqliteStepStore(connection=step_conn)
        try:
            yield conv_store, ticket_store, step_store
        finally:
            await engine.dispose()
            step_conn.close()

    async def test_root_lifecycle_and_workspace_binding(self) -> None:
        async with self._stores() as (conv_store, _, _):
            root1 = await conv_store.ensure_root_node()
            root2 = await conv_store.ensure_root_node()
            self.assertEqual(root1, root2)

            record = await conv_store.create_conversation_from_root(
                target_title="新对话"
            )
            self.assertEqual(
                await conv_store.get_last_active_conversation_id(),
                record.conversation_id,
            )

            await conv_store.bind_workspace(record.conversation_id, "/tmp/demo-repo")
            sessions = create_session_factory(conv_store._engine)  # pyright: ignore[reportPrivateUsage]
            async with sessions() as session:
                row = await session.get(ConversationRow, record.conversation_id)
                assert row is not None
                self.assertEqual(row.work_path, "/tmp/demo-repo")

    async def test_save_load_history_and_stop_backtrack(self) -> None:
        async with self._stores() as (conv_store, _, _):
            record = await conv_store.create_conversation_from_root()
            conv = await conv_store.load(record.conversation_id, scope="main")
            assert conv is not None
            self.assertEqual(conv.messages, [])

            conv.messages.extend(
                [_user_req("第一轮"), _assistant_resp("回复1"), _user_req("第二轮")]
            )
            saved_entries = await conv_store.save(conv, scope="main")
            self.assertEqual(len(saved_entries), 3)

            reloaded = await conv_store.load(record.conversation_id, scope="main")
            assert reloaded is not None
            self.assertEqual(len(reloaded.messages), 3)

            recent = await conv_store.read_history(saved_entries[-1].id, limit=2)
            self.assertEqual(len(recent), 2)
            self.assertEqual(recent[0].id, saved_entries[1].id)

            handoff_id = "handoff-entry-1"
            now_iso = datetime.now(UTC).isoformat()
            sessions = create_session_factory(conv_store._engine)  # pyright: ignore[reportPrivateUsage]
            async with sessions() as session, session.begin():
                session.add(
                    ContextEntryRow(
                        id=handoff_id,
                        tree_id="default",
                        parent_id=saved_entries[1].id,
                        kind="handoff",
                        stop_backtrack=1,
                        message_json=None,
                        created_at=now_iso,
                    )
                )

            truncated = await conv_store.read_history(handoff_id)
            self.assertEqual(truncated, [])

    async def test_optimistic_lock_fork_and_delete_gc(self) -> None:
        async with self._stores() as (conv_store, _, _):
            record = await conv_store.create_conversation_from_root()
            c_a = await conv_store.load(record.conversation_id, scope="main")
            c_b = await conv_store.load(record.conversation_id, scope="main")
            assert c_a is not None and c_b is not None

            c_a.messages.append(_user_req("A 先写"))
            _ = await conv_store.save(c_a, scope="main")

            c_b.messages.append(_user_req("B 并发写"))
            with self.assertRaises(ConversationConflict):
                _ = await conv_store.save(c_b, scope="main")

            forked = await conv_store.fork(
                record.conversation_id,
                scope=f"conv:{record.conversation_id}",
                source_scope="main",
            )
            forked.messages.append(_user_req("Role 子任务消息"))
            _ = await conv_store.save(forked, scope=f"conv:{record.conversation_id}")

            assert forked.revision is not None
            await conv_store.delete(
                forked.conversation_id, scope=f"conv:{record.conversation_id}"
            )
            main_after = await conv_store.load(record.conversation_id, scope="main")
            assert main_after is not None
            self.assertEqual(len(main_after.messages), 1)
            self.assertIsNotNone(await conv_store.ensure_root_node())

    async def test_ticket_store_and_restart_recovery(self) -> None:
        async with self._stores() as (conv_store, ticket_store, step_store):
            main_rec = await conv_store.create_conversation_from_root(
                work_path="/repo/a"
            )
            main_id = main_rec.conversation_id

            t1_id = await ticket_store.next_task_id(main_id)
            t1 = await ticket_store.create_ticket(
                main_conversation_id=main_id,
                task_id=t1_id,
                role_name="coder",
                mandate="重构鉴权",
                anchor_entry_id=main_rec.head_id,
                work_path="/repo/a",
            )
            self.assertEqual(t1.task_id, "T1")

            t2_id = await ticket_store.next_task_id(main_id)
            _ = await ticket_store.create_ticket(
                main_conversation_id=main_id,
                task_id=t2_id,
                role_name="tester",
                mandate="同目录排队单测",
                anchor_entry_id=main_rec.head_id,
                work_path="/repo/a",
            )
            t3_id = await ticket_store.next_task_id(main_id)
            _ = await ticket_store.create_ticket(
                main_conversation_id=main_id,
                task_id=t3_id,
                role_name="researcher",
                mandate="异目录调研",
                anchor_entry_id=main_rec.head_id,
                work_path="/repo/b",
            )

            role_conv = await conv_store.fork(
                main_id, scope=f"conv:{main_id}", source_scope="main"
            )
            await ticket_store.register_role_conversation(
                main_conversation_id=main_id,
                task_id="T1",
                role_name="coder",
                role_conversation_id=role_conv.conversation_id,
            )
            self.assertEqual(
                await ticket_store.get_role_conversation_id(main_id, "T1"),
                role_conv.conversation_id,
            )

            run_id = "run-t1-crash"
            _ = await ticket_store.update_ticket_status(
                main_id, "T1", status="running", run_id=run_id
            )
            await step_store.register_run(
                RunRecord(
                    run_id=run_id,
                    conversation_id=role_conv.conversation_id,
                    agent_name="coder",
                )
            )
            unresolved_msgs = [
                _user_req("重构鉴权"),
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="edit_file",
                            args={"path": "auth.py"},
                            tool_call_id="call-1",
                        )
                    ]
                ),
            ]
            await step_store.save_snapshot(
                ContinuableSnapshot(
                    run_id=run_id,
                    step_index=1,
                    messages=unresolved_msgs,
                    conversation_id=role_conv.conversation_id,
                    agent_name="coder",
                    state="interrupted",
                )
            )

            (
                interrupted_ids,
                auto_requeue_ids,
            ) = await ticket_store.recover_main_conversation_tasks(
                main_id, conversation_store=conv_store, step_store=step_store
            )
            self.assertEqual(interrupted_ids, ["T1"])
            self.assertEqual(auto_requeue_ids, ["T3"])

            t1_after = await ticket_store.get_ticket(main_id, "T1")
            assert t1_after is not None
            self.assertEqual(t1_after.status, "interrupted")

            repaired_conv = await conv_store.load(
                role_conv.conversation_id, scope=f"conv:{main_id}"
            )
            assert repaired_conv is not None
            last_msg = repaired_conv.messages[-1]
            self.assertIsInstance(last_msg, ModelRequest)
            assert isinstance(last_msg, ModelRequest)
            self.assertTrue(
                any(isinstance(part, ToolReturnPart) for part in last_msg.parts)
            )


if __name__ == "__main__":
    _ = unittest.main()
