"""基于 SQLModel + aiosqlite 的 Ticket、Role 子会话索引与重启恢复仓库。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict, ConversationStore
from pydantic_ai.messages import ModelMessage, repair_messages
from pydantic_ai_harness.step_persistence import StepStore
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlmodel import col, select

from agent.db.engine import create_session_factory
from agent.db.models import ConversationRow, TicketRow


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def paths_overlap(path_a: str | Path, path_b: str | Path) -> bool:
    """判断两个规范化目录路径是否存在相同或父子嵌套重叠。"""
    pa = Path(path_a).expanduser().resolve()
    pb = Path(path_b).expanduser().resolve()
    return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)


def parse_depends_on(depends_on_json: str) -> tuple[str, ...]:
    """解析 tickets.depends_on_json 为标准化的前置 task_id 元组。"""
    try:
        raw: object = json.loads(depends_on_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip().lstrip("@") for item in raw if str(item).strip())


class SQLModelTicketStore:
    """管理 tickets 持久化、@task_id -> role_conversation_id 映射及重启恢复。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine: AsyncEngine = engine
        self._sessions: async_sessionmaker[AsyncSession] = session_factory or create_session_factory(engine)

    async def next_task_id(self, main_conversation_id: str) -> str:
        """为指定主对话生成递增可读任务 ID（T1, T2, ...）。"""
        async with self._sessions() as session:
            stmt = select(TicketRow.task_id).where(col(TicketRow.main_conversation_id) == main_conversation_id)
            task_ids = (await session.execute(stmt)).scalars().all()
        max_num = 0
        for tid in task_ids:
            if tid.startswith("T") and tid[1:].isdigit():
                max_num = max(max_num, int(tid[1:]))
        return f"T{max_num + 1}"

    async def create_ticket(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        role_name: str,
        mandate: str,
        anchor_entry_id: str | None = None,
        work_path: str | None = None,
        lock_mode: str = "exclusive",
        depends_on: Sequence[str] = (),
        status: str = "queued",
    ) -> TicketRow:
        """创建并持久化一条任务单记录。"""
        now_iso = _utc_iso()
        norm_deps = [d.strip().lstrip("@") for d in depends_on if d.strip()]
        row = TicketRow(
            main_conversation_id=main_conversation_id,
            task_id=task_id,
            role_name=role_name,
            anchor_entry_id=anchor_entry_id,
            work_path=work_path,
            lock_mode=lock_mode,
            depends_on_json=json.dumps(norm_deps),
            mandate=mandate,
            status=status,
            created_at=now_iso,
            updated_at=now_iso,
        )
        async with self._sessions() as session:
            async with session.begin():
                session.add(row)
        return row

    async def get_ticket(self, main_conversation_id: str, task_id: str) -> TicketRow | None:
        async with self._sessions() as session:
            return await session.get(TicketRow, (main_conversation_id, task_id))

    async def list_tickets(self, main_conversation_id: str, *, status: str | None = None) -> list[TicketRow]:
        async with self._sessions() as session:
            stmt = (
                select(TicketRow)
                .where(col(TicketRow.main_conversation_id) == main_conversation_id)
                .order_by(col(TicketRow.created_at).asc())
            )
            if status is not None:
                stmt = stmt.where(col(TicketRow.status) == status)
            return list((await session.execute(stmt)).scalars().all())

    async def update_ticket_status(
        self,
        main_conversation_id: str,
        task_id: str,
        *,
        status: str,
        run_id: str | None = None,
        result_summary: str | None = None,
        mandate: str | None = None,
    ) -> TicketRow:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(TicketRow, (main_conversation_id, task_id))
                if row is None:
                    raise LookupError(f"Ticket ({main_conversation_id!r}, {task_id!r}) not found.")
                row.status = status
                if run_id is not None:
                    row.run_id = run_id
                if result_summary is not None:
                    row.result_summary = result_summary
                if mandate is not None:
                    row.mandate = mandate
                row.updated_at = _utc_iso()
                return row

    async def remove_dependency(self, main_conversation_id: str, cancelled_task_id: str) -> None:
        """当上游任务被显式取消时，自动从同会话下游任务的持久化依赖中移除该 ID。"""
        target = cancelled_task_id.strip().lstrip("@")
        async with self._sessions() as session:
            async with session.begin():
                stmt = select(TicketRow).where(col(TicketRow.main_conversation_id) == main_conversation_id)
                rows = (await session.execute(stmt)).scalars().all()
                now_iso = _utc_iso()
                for row in rows:
                    deps = parse_depends_on(row.depends_on_json)
                    if target in deps:
                        row.depends_on_json = json.dumps([d for d in deps if d != target])
                        row.updated_at = now_iso

    async def get_role_conversation_id(self, main_conversation_id: str, task_id: str) -> str | None:
        row = await self.get_ticket(main_conversation_id, task_id)
        return row.role_conversation_id if row is not None else None

    async def register_role_conversation(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        role_name: str,
        role_conversation_id: str,
    ) -> None:
        """绑定 @task_id 与专属 Role conversation_id，并同步补全 conversations 元数据。"""
        now_iso = _utc_iso()
        async with self._sessions() as session:
            async with session.begin():
                ticket = await session.get(TicketRow, (main_conversation_id, task_id))
                if ticket is None:
                    raise LookupError(f"Ticket ({main_conversation_id!r}, {task_id!r}) not found.")
                ticket.role_conversation_id = role_conversation_id
                ticket.updated_at = now_iso

                conv = await session.get(ConversationRow, role_conversation_id)
                if conv is not None:
                    conv.role_name = role_name
                    conv.work_path = ticket.work_path
                    conv.updated_at = now_iso

    async def mark_ticket_interrupted(
        self, *, main_conversation_id: str, task_id: str, summary: str
    ) -> TicketRow:
        return await self.update_ticket_status(
            main_conversation_id,
            task_id,
            status="interrupted",
            result_summary=summary,
        )

    async def recover_main_conversation_tasks(
        self,
        main_conversation_id: str,
        *,
        conversation_store: ConversationStore,
        step_store: StepStore,
    ) -> tuple[list[str], list[str]]:
        """重启恢复指定主会话的 tickets：修补运行中断任务，返回 (interrupted_ids, auto_requeue_ids)。"""
        rows = await self.list_tickets(main_conversation_id)
        interrupted_dirs: set[str] = set()
        blocked_upstream_ids: set[str] = set()
        interrupted_ids: list[str] = []
        auto_requeue_ids: list[str] = []

        for row in rows:
            if row.status == "running":
                await self._repair_running_task(
                    main_conversation_id=main_conversation_id,
                    row=row,
                    conversation_store=conversation_store,
                    step_store=step_store,
                )
                if row.work_path is not None:
                    interrupted_dirs.add(row.work_path)
                blocked_upstream_ids.add(row.task_id)
                interrupted_ids.append(row.task_id)
            elif row.status in ("failed", "interrupted"):
                if row.status == "interrupted" and row.work_path is not None:
                    interrupted_dirs.add(row.work_path)
                blocked_upstream_ids.add(row.task_id)
            elif row.status == "queued":
                deps = parse_depends_on(row.depends_on_json)
                has_blocked_dep = any(dep in blocked_upstream_ids for dep in deps)
                dir_blocked = row.work_path is not None and any(
                    paths_overlap(row.work_path, interrupted_dir) for interrupted_dir in interrupted_dirs
                )
                if not has_blocked_dep and not dir_blocked:
                    auto_requeue_ids.append(row.task_id)

        return interrupted_ids, auto_requeue_ids

    async def _repair_running_task(
        self,
        *,
        main_conversation_id: str,
        row: TicketRow,
        conversation_store: ConversationStore,
        step_store: StepStore,
    ) -> None:
        if row.role_conversation_id is not None and row.run_id is not None:
            snapshot = await step_store.latest_snapshot(run_id=row.run_id, include_interrupted=True)
            if snapshot is not None:
                repaired: list[ModelMessage] = repair_messages(snapshot.messages)
                scope = f"conv:{main_conversation_id}"
                conv = await conversation_store.load(row.role_conversation_id, scope=scope)
                if conv is not None:
                    merged = Conversation(
                        messages=repaired,
                        usage=conv.usage,
                        conversation_id=conv.conversation_id,
                        deferred_tool_requests=conv.deferred_tool_requests,
                        revision=conv.revision,
                    )
                    try:
                        _ = await conversation_store.save(merged, scope=scope)
                    except ConversationConflict:
                        pass

        _ = await self.mark_ticket_interrupted(
            main_conversation_id=main_conversation_id,
            task_id=row.task_id,
            summary="进程退出导致任务中断，已保留断点上下文，可 @task_id 确认后继续。",
        )
