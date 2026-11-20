"""基于 SQLModel + aiosqlite 的 Context Tree ConversationStore 实现。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from pydantic_ai._uuid import uuid7
from pydantic_ai.conversation_store import (
    ConversationConflict,
    ConversationEntry,
    ConversationRecord,
    ConversationStore,
)
from pydantic_ai.messages import ModelMessage
from pydantic_ai_harness.media._store import MediaStore
from sqlalchemy import delete, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlmodel import col, select

from agent.db._conv_helpers import (
    BRANCHES_ADAPTER as _BRANCHES_ADAPTER,
)
from agent.db._conv_helpers import (
    DEFERRED_ADAPTER as _DEFERRED_ADAPTER,
)
from agent.db._conv_helpers import (
    USAGE_ADAPTER as _USAGE_ADAPTER,
)
from agent.db._conv_helpers import (
    derive_parent_conv_id as _derive_parent_conv_id,
)
from agent.db._conv_helpers import (
    dump_message_async as _dump_message_async,
)
from agent.db._conv_helpers import (
    gc_unreachable_entries,
)
from agent.db._conv_helpers import (
    load_message_async as _load_message_async,
)
from agent.db._conv_helpers import (
    row_to_record as _row_to_record,
)
from agent.db._conv_helpers import (
    utc_iso as _utc_iso,
)
from agent.db.engine import create_session_factory
from agent.db.media_store import (
    DEFAULT_MEDIA_THRESHOLD_BYTES,
    DEFAULT_MEDIA_WINDOW_MESSAGES,
)
from agent.db.models import AppStateRow, ContextEntryRow, ConversationRow

DEFAULT_TREE_ID = "default"
ROOT_ENTRY_KEY = "root_entry_id"
LAST_ACTIVE_CONV_KEY = "last_active_conversation_id"


class SQLModelConversationStore(ConversationStore):
    """基于 SQLModel + aiosqlite 的异步父指针链 ConversationStore（支持 OPT-3/4 外置与 OPT-6 窗口还原）。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        tree_id: str = DEFAULT_TREE_ID,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        media_store: MediaStore | None = None,
        media_threshold_bytes: int = DEFAULT_MEDIA_THRESHOLD_BYTES,
        media_window: int | None = DEFAULT_MEDIA_WINDOW_MESSAGES,
    ) -> None:
        self._engine: AsyncEngine = engine
        self._tree_id: str = tree_id
        self._sessions: async_sessionmaker[AsyncSession] = (
            session_factory or create_session_factory(engine)
        )
        self._media_store: MediaStore | None = media_store
        self._media_threshold_bytes: int = media_threshold_bytes
        self._media_window: int | None = media_window

    @property
    def media_store(self) -> MediaStore | None:
        return self._media_store

    async def read_record(self, conversation_id: str) -> ConversationRecord | None:
        async with self._sessions() as session:
            row = await session.get(ConversationRow, conversation_id)
            return _row_to_record(row) if row is not None else None

    async def read_records(
        self, *, scope: str | None = None, limit: int | None = None
    ) -> Sequence[ConversationRecord]:
        async with self._sessions() as session:
            stmt = select(ConversationRow).order_by(
                col(ConversationRow.updated_at).desc()
            )
            if scope is not None:
                stmt = stmt.where(col(ConversationRow.scope) == scope)
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return [_row_to_record(row) for row in result.scalars().all()]

    async def write_record(
        self,
        record: ConversationRecord,
        *,
        new_entries: Sequence[ConversationEntry] = (),
        expected_revision: str | None,
    ) -> None:
        new_ids = {entry.id for entry in new_entries}
        now_iso = _utc_iso(record.updated_at)
        usage_json = _USAGE_ADAPTER.dump_json(record.usage).decode("utf-8")
        deferred_json = (
            _DEFERRED_ADAPTER.dump_json(record.deferred_tool_requests).decode("utf-8")
            if record.deferred_tool_requests is not None
            else None
        )
        branches_json = _BRANCHES_ADAPTER.dump_json(record.branches).decode("utf-8")

        async with self._sessions() as session, session.begin():
            if record.head_id is not None and record.head_id not in new_ids:
                head_row = await session.get(ContextEntryRow, record.head_id)
                if head_row is None:
                    raise ConversationConflict(record.conversation_id)

            for entry in new_entries:
                dumped_msg = await _dump_message_async(
                    entry.message,
                    media_store=self._media_store,
                    threshold_bytes=self._media_threshold_bytes,
                )
                session.add(
                    ContextEntryRow(
                        id=entry.id,
                        tree_id=self._tree_id,
                        parent_id=entry.parent_id,
                        kind=entry.kind,
                        stop_backtrack=0,
                        message_json=dumped_msg,
                        created_at=_utc_iso(),
                    )
                )

            if expected_revision is None:
                row = ConversationRow(
                    conversation_id=record.conversation_id,
                    tree_id=self._tree_id,
                    parent_conversation_id=_derive_parent_conv_id(record.scope),
                    scope=record.scope,
                    head_entry_id=record.head_id,
                    forked_from=record.forked_from,
                    forked_at=record.forked_at,
                    branch_mode="full_fork" if record.forked_from else "root",
                    branches_json=branches_json,
                    usage_json=usage_json,
                    deferred_json=deferred_json,
                    revision=record.revision,
                    updated_at=now_iso,
                )
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ConversationConflict(record.conversation_id) from exc
            else:
                stmt = (
                    update(ConversationRow)
                    .where(
                        col(ConversationRow.conversation_id) == record.conversation_id,
                        col(ConversationRow.revision) == expected_revision,
                    )
                    .values(
                        head_entry_id=record.head_id,
                        scope=record.scope,
                        branches_json=branches_json,
                        usage_json=usage_json,
                        deferred_json=deferred_json,
                        revision=record.revision,
                        updated_at=now_iso,
                    )
                )
                res = cast(CursorResult[object], await session.execute(stmt))
                if res.rowcount != 1:
                    raise ConversationConflict(record.conversation_id)

    async def delete_record(
        self, conversation_id: str, *, expected_revision: str
    ) -> None:
        async with self._sessions() as session, session.begin():
            stmt = delete(ConversationRow).where(
                col(ConversationRow.conversation_id) == conversation_id,
                col(ConversationRow.revision) == expected_revision,
            )
            res = cast(CursorResult[object], await session.execute(stmt))
            if res.rowcount != 1:
                raise ConversationConflict(conversation_id)
            await gc_unreachable_entries(session, tree_id=self._tree_id)

    async def read_entry(self, entry_id: str) -> ConversationEntry:
        async with self._sessions() as session:
            row = await session.get(ContextEntryRow, entry_id)
            if row is None or row.message_json is None:
                raise LookupError(f"Entry {entry_id!r} not found.")
            msg = await _load_message_async(
                row.message_json,
                media_store=self._media_store,
                hydrate_media=True,
            )
            return ConversationEntry(
                id=row.id,
                parent_id=row.parent_id,
                message=msg,
            )

    async def read_history(
        self,
        head_id: str | None,
        *,
        limit: int | None = None,
        media_window: int | None = None,
    ) -> list[ConversationEntry]:
        """使用递归 CTE 一次性从 head_id 回溯到根/截断节点，SQL 下推 LIMIT 并按 media_window 还原最近 K 条大附件（OPT-6）。"""
        if head_id is None:
            return []

        limit_clause = "LIMIT :limit" if limit is not None else ""
        sql = text(f"""
            WITH RECURSIVE branch_chain AS (
                SELECT id, parent_id, kind, stop_backtrack, message_json, 0 AS depth
                FROM context_entries
                WHERE id = :head_id

                UNION ALL

                SELECT e.id, e.parent_id, e.kind, e.stop_backtrack, e.message_json, bc.depth + 1
                FROM context_entries e
                JOIN branch_chain bc ON e.id = bc.parent_id
                WHERE bc.stop_backtrack = 0 OR e.kind = 'root'
            )
            SELECT id, parent_id, kind, message_json, depth
            FROM branch_chain
            WHERE message_json IS NOT NULL
            ORDER BY depth ASC
            {limit_clause}
        """)
        params: dict[str, object] = {"head_id": head_id}
        if limit is not None:
            params["limit"] = max(0, limit)
        async with self._sessions() as session:
            rows = (await session.execute(sql, params)).mappings().all()
            if not rows:
                if await session.get(ContextEntryRow, head_id) is None:
                    raise LookupError(f"Entry {head_id!r} not found.")
                return []

        effective_window = self._media_window if media_window is None else media_window
        entries: list[ConversationEntry] = []
        for row in rows:
            raw_msg = row["message_json"]
            if raw_msg is None:
                continue
            should_hydrate = effective_window is None or len(entries) < effective_window
            msg = await _load_message_async(
                str(raw_msg), media_store=self._media_store, hydrate_media=should_hydrate
            )
            entries.append(
                ConversationEntry(
                    id=str(row["id"]),
                    parent_id=str(row["parent_id"]) if row["parent_id"] is not None else None,
                    message=msg,
                )
            )

        entries.reverse()
        return entries

    async def get_app_state(self, key: str) -> str | None:
        async with self._sessions() as session:
            row = await session.get(AppStateRow, key)
            return row.value if row is not None else None

    async def set_app_state(self, key: str, value: str) -> None:
        now_iso = _utc_iso()
        async with self._sessions() as session, session.begin():
            row = await session.get(AppStateRow, key)
            if row is None:
                session.add(AppStateRow(key=key, value=value, updated_at=now_iso))
            else:
                row.value = value
                row.updated_at = now_iso

    async def ensure_root_node(self) -> str:
        """幂等创建全局唯一的 Root 元祖节点，并记录到 app_state。"""
        existing_id = await self.get_app_state(ROOT_ENTRY_KEY)
        if existing_id is not None:
            return existing_id

        root_id = str(uuid7())
        now_iso = _utc_iso()
        async with self._sessions() as session, session.begin():
            session.add(
                ContextEntryRow(
                    id=root_id,
                    tree_id=self._tree_id,
                    parent_id=None,
                    kind="root",
                    stop_backtrack=0,
                    message_json=None,
                    created_at=now_iso,
                )
            )
            session.add(
                AppStateRow(key=ROOT_ENTRY_KEY, value=root_id, updated_at=now_iso)
            )
        return root_id

    async def create_conversation_from_root(
        self,
        *,
        conversation_id: str | None = None,
        work_path: str | None = None,
        scope: str = "main",
        target_title: str = "",
    ) -> ConversationRecord:
        """从 Root Node 派生新会话的 conv_init 锚点与 conversations 游标。"""
        root_id = await self.ensure_root_node()
        conv_id = conversation_id or str(uuid7())
        init_entry_id = str(uuid7())
        revision = str(uuid7())
        now = datetime.now(UTC)
        now_iso = _utc_iso(now)

        async with self._sessions() as session, session.begin():
            session.add(
                ContextEntryRow(
                    id=init_entry_id,
                    tree_id=self._tree_id,
                    parent_id=root_id,
                    kind="conv_init",
                    stop_backtrack=0,
                    message_json=None,
                    created_at=now_iso,
                )
            )
            session.add(
                ConversationRow(
                    conversation_id=conv_id,
                    tree_id=self._tree_id,
                    parent_conversation_id=_derive_parent_conv_id(scope),
                    scope=scope,
                    head_entry_id=init_entry_id,
                    branch_mode="root_only",
                    work_path=work_path,
                    target_title=target_title,
                    revision=revision,
                    updated_at=now_iso,
                )
            )
            if scope == "main":
                state_row = await session.get(AppStateRow, LAST_ACTIVE_CONV_KEY)
                if state_row is None:
                    session.add(
                        AppStateRow(
                            key=LAST_ACTIVE_CONV_KEY,
                            value=conv_id,
                            updated_at=now_iso,
                        )
                    )
                else:
                    state_row.value = conv_id
                    state_row.updated_at = now_iso

        return ConversationRecord(
            conversation_id=conv_id,
            head_id=init_entry_id,
            scope=scope,
            revision=revision,
            updated_at=now,
        )

    async def bind_workspace(self, conversation_id: str, work_path: str | None) -> None:
        """更新指定会话绑定的规范化工作区路径。"""
        async with self._sessions() as session, session.begin():
            row = await session.get(ConversationRow, conversation_id)
            if row is None:
                raise LookupError(f"Conversation {conversation_id!r} not found.")
            row.work_path = work_path
            row.updated_at = _utc_iso()

    async def get_work_path(self, conversation_id: str) -> str | None:
        """读取指定会话绑定的规范化工作区路径。"""
        async with self._sessions() as session:
            row = await session.get(ConversationRow, conversation_id)
            if row is None:
                raise LookupError(f"Conversation {conversation_id!r} not found.")
            return row.work_path

    async def create_handoff_branch(
        self,
        parent_conversation_id: str,
        *,
        handoff_message: ModelMessage,
        work_path: str | None = None,
        target_title: str = "",
        scope: str = "main",
    ) -> ConversationRecord:
        """在父分支当前末端插入 stop_backtrack=1 的交接节点并派生新分支。"""
        parent_rec = await self.read_record(parent_conversation_id)
        if parent_rec is None:
            raise LookupError(f"Conversation {parent_conversation_id!r} not found.")

        handoff_id = str(uuid7())
        child_id = str(uuid7())
        revision = str(uuid7())
        now = datetime.now(UTC)
        now_iso = _utc_iso(now)

        dumped_handoff = await _dump_message_async(
            handoff_message,
            media_store=self._media_store,
            threshold_bytes=self._media_threshold_bytes,
        )
        async with self._sessions() as session, session.begin():
            session.add(
                ContextEntryRow(
                    id=handoff_id,
                    tree_id=self._tree_id,
                    parent_id=parent_rec.head_id,
                    kind="handoff",
                    stop_backtrack=1,
                    message_json=dumped_handoff,
                    created_at=now_iso,
                )
            )
            session.add(
                ConversationRow(
                    conversation_id=child_id,
                    tree_id=self._tree_id,
                    parent_conversation_id=_derive_parent_conv_id(scope),
                    scope=scope,
                    head_entry_id=handoff_id,
                    forked_from=parent_conversation_id,
                    forked_at=parent_rec.head_id,
                    branch_mode="handoff",
                    work_path=work_path,
                    target_title=target_title,
                    revision=revision,
                    updated_at=now_iso,
                )
            )
            if scope == "main":
                state_row = await session.get(AppStateRow, LAST_ACTIVE_CONV_KEY)
                if state_row is None:
                    session.add(AppStateRow(key=LAST_ACTIVE_CONV_KEY, value=child_id, updated_at=now_iso))
                else:
                    state_row.value = child_id
                    state_row.updated_at = now_iso

        return ConversationRecord(
            conversation_id=child_id,
            head_id=handoff_id,
            scope=scope,
            forked_from=parent_conversation_id,
            forked_at=parent_rec.head_id,
            revision=revision,
            updated_at=now,
        )

    async def list_main_branches(self, *, limit: int = 20) -> list[ConversationRow]:
        """按最近更新时间倒序列出主会话分支记录。"""
        async with self._sessions() as session:
            stmt = (
                select(ConversationRow)
                .where(col(ConversationRow.scope) == "main")
                .order_by(col(ConversationRow.updated_at).desc())
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def update_target_title(self, conversation_id: str, target_title: str) -> None:
        """更新指定会话的目标摘要。"""
        async with self._sessions() as session, session.begin():
            row = await session.get(ConversationRow, conversation_id)
            if row is None:
                raise LookupError(f"Conversation {conversation_id!r} not found.")
            row.target_title = target_title
            row.updated_at = _utc_iso()

    async def get_last_active_conversation_id(self) -> str | None:
        return await self.get_app_state(LAST_ACTIVE_CONV_KEY)
