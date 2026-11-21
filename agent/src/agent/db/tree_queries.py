"""SQLModel 协议扩展读模型与快照存储：Tree 双层懒加载、Context 快照与 Jev 辅助用量。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    RetryFeedbackPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col, select

from agent.db._conv_helpers import USAGE_ADAPTER as _USAGE_ADAPTER
from agent.db._conv_helpers import load_message_async, utc_iso
from agent.db.conversation_store import LAST_ACTIVE_CONV_KEY, ROOT_ENTRY_KEY
from agent.db.models import (
    AppStateRow,
    ContextEntryRow,
    ContextSnapshotRow,
    ConversationAuxUsageRow,
    ConversationRow,
    TicketRow,
)

_ATT_HANDLE_RE = re.compile(r"\[\[attachment:(att_[a-zA-Z0-9]+)")
_EXT_ID_RE = re.compile(r"\bid=(att_[a-zA-Z0-9]+)\b")


@dataclass(frozen=True, kw_only=True)
class TreeBranchRecord:
    row: ConversationRow
    task_id: str | None = None
    task_status: str | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    message_count: int = 0
    active_task_count: int = 0


@dataclass(frozen=True, kw_only=True)
class TreeTopologyData:
    root_entry_id: str | None
    active_conversation_id: str | None
    branches: list[TreeBranchRecord]


@dataclass(frozen=True, kw_only=True)
class EntryPartData:
    part_kind: str
    role: Literal["system", "user", "assistant", "tool"]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    attachment_id: str | None = None
    content_url: str | None = None


@dataclass(frozen=True, kw_only=True)
class EntryRecordData:
    entry_id: str
    parent_id: str | None
    kind: Literal["root", "conv_init", "message", "handoff"]
    stop_backtrack: bool
    run_id: str | None
    parts: list[EntryPartData] = field(default_factory=list)
    child_branch_ids: list[str] = field(default_factory=list)
    attached_task_ids: list[str] = field(default_factory=list)
    created_at: str = ""


@dataclass(frozen=True, kw_only=True)
class EntryPageData:
    conversation_id: str
    entries: list[EntryRecordData]
    has_more: bool
    next_before_entry_id: str | None


def _extract_attachment_id(obj: object) -> str | None:
    ident = getattr(obj, "identifier", None)
    if isinstance(ident, str) and ident.startswith("att_"):
        return ident
    text_cand = obj if isinstance(obj, str) else getattr(obj, "content", None)
    if isinstance(text_cand, str):
        m = _ATT_HANDLE_RE.search(text_cand) or _EXT_ID_RE.search(text_cand)
        if m:
            return m.group(1)
    return None


def _message_to_part_data(msg: ModelMessage) -> list[EntryPartData]:
    out: list[EntryPartData] = []
    is_req = isinstance(msg, ModelRequest)
    for part in msg.parts:
        if isinstance(part, SystemPromptPart):
            out.append(EntryPartData(part_kind=part.part_kind, role="system", text=str(part.content)))
        elif isinstance(part, UserPromptPart):
            if isinstance(part.content, str):
                att_id = _extract_attachment_id(part.content)
                out.append(
                    EntryPartData(
                        part_kind=part.part_kind,
                        role="user",
                        text=part.content,
                        attachment_id=att_id,
                        content_url=f"/v1/attachments/{att_id}/content" if att_id else None,
                    )
                )
            else:
                for item in part.content:
                    att_id = _extract_attachment_id(item)
                    text_val = item if isinstance(item, str) else getattr(item, "content", None)
                    out.append(
                        EntryPartData(
                            part_kind=getattr(item, "kind", "user-content"),
                            role="user",
                            text=text_val if isinstance(text_val, str) else None,
                            attachment_id=att_id,
                            content_url=f"/v1/attachments/{att_id}/content" if att_id else None,
                        )
                    )
        elif isinstance(part, (TextPart, ThinkingPart)):
            out.append(EntryPartData(part_kind=part.part_kind, role="assistant", text=part.content))
        elif isinstance(part, ToolCallPart):
            out.append(
                EntryPartData(
                    part_kind=part.part_kind,
                    role="assistant",
                    text=part.args_as_json_str()[:300],
                    tool_name=part.tool_name,
                    tool_call_id=part.tool_call_id,
                )
            )
        elif isinstance(part, (ToolReturnPart, RetryFeedbackPart)):
            out.append(
                EntryPartData(
                    part_kind=part.part_kind,
                    role="tool",
                    text=str(part.content)[:400],
                    tool_name=getattr(part, "tool_name", None),
                    tool_call_id=getattr(part, "tool_call_id", None),
                )
            )
        else:
            out.append(
                EntryPartData(
                    part_kind=getattr(part, "part_kind", "unknown"),
                    role="user" if is_req else "assistant",
                )
            )
    return out


class SQLModelProtocolStore:
    """面向 GUI 协议层的只读树查询、Context 快照与辅助用量持久化仓库。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = session_factory

    async def read_tree_topology(
        self, *, include_role_branches: bool = True, limit: int = 200
    ) -> TreeTopologyData:
        async with self._sessions() as session:
            root_row = await session.get(AppStateRow, ROOT_ENTRY_KEY)
            active_row = await session.get(AppStateRow, LAST_ACTIVE_CONV_KEY)
            stmt = select(ConversationRow).order_by(col(ConversationRow.updated_at).desc()).limit(limit)
            if not include_role_branches:
                stmt = stmt.where(col(ConversationRow.scope) == "main")
            convs = list((await session.execute(stmt)).scalars().all())
            tickets = list((await session.execute(select(TicketRow))).scalars().all())
            entry_rows = (
                await session.execute(
                    select(
                        col(ContextEntryRow.id),
                        col(ContextEntryRow.parent_id),
                        col(ContextEntryRow.kind),
                        col(ContextEntryRow.stop_backtrack),
                    )
                )
            ).all()

        entry_meta: dict[str, tuple[str | None, str, int]] = {
            str(r[0]): (str(r[1]) if r[1] is not None else None, str(r[2]), int(r[3]))
            for r in entry_rows
        }

        def _count_branch_messages(head_id: str | None) -> int:
            count = 0
            cur = head_id
            seen: set[str] = set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                meta = entry_meta.get(cur)
                if meta is None:
                    break
                parent_id, kind, stop_bt = meta
                if kind == "message":
                    count += 1
                if stop_bt == 1:
                    break
                cur = parent_id
            return count

        role_to_task: dict[str, tuple[str, str]] = {}
        active_tasks_by_main: dict[str, int] = {}
        for t in tickets:
            if t.role_conversation_id:
                role_to_task[t.role_conversation_id] = (t.task_id, t.status)
            if t.status in ("queued", "running"):
                active_tasks_by_main[t.main_conversation_id] = (
                    active_tasks_by_main.get(t.main_conversation_id, 0) + 1
                )

        records: list[TreeBranchRecord] = []
        for c in convs:
            task_info = role_to_task.get(c.conversation_id)
            usage = _USAGE_ADAPTER.validate_json(c.usage_json)
            records.append(
                TreeBranchRecord(
                    row=c,
                    task_id=task_info[0] if task_info is not None else None,
                    task_status=task_info[1] if task_info is not None else None,
                    usage=usage,
                    message_count=_count_branch_messages(c.head_entry_id),
                    active_task_count=active_tasks_by_main.get(c.conversation_id, 0),
                )
            )
        return TreeTopologyData(
            root_entry_id=root_row.value if root_row is not None else None,
            active_conversation_id=active_row.value if active_row is not None else None,
            branches=records,
        )

    async def read_entry_page(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        before_entry_id: str | None = None,
    ) -> EntryPageData:
        async with self._sessions() as session:
            conv_row = await session.get(ConversationRow, conversation_id)
            if conv_row is None:
                raise LookupError(f"Conversation {conversation_id!r} not found.")
            start_id = before_entry_id or conv_row.head_entry_id
            if start_id is None:
                return EntryPageData(
                    conversation_id=conversation_id, entries=[], has_more=False, next_before_entry_id=None
                )

            sql = text("""
                WITH RECURSIVE branch_chain AS (
                    SELECT id, parent_id, kind, stop_backtrack, message_json, run_id, created_at, 0 AS depth
                    FROM context_entries
                    WHERE id = :start_id
                    UNION ALL
                    SELECT e.id, e.parent_id, e.kind, e.stop_backtrack, e.message_json, e.run_id, e.created_at, bc.depth + 1
                    FROM context_entries e
                    JOIN branch_chain bc ON e.id = bc.parent_id
                    WHERE bc.stop_backtrack = 0 OR e.kind = 'root'
                )
                SELECT id, parent_id, kind, stop_backtrack, message_json, run_id, created_at
                FROM branch_chain
                ORDER BY depth ASC
                LIMIT :fetch_limit
            """)
            rows = cast(
                Sequence[Mapping[str, object]],
                (await session.execute(sql, {"start_id": start_id, "fetch_limit": max(1, limit) + 1}))
                .mappings()
                .all(),
            )
            forked_rows = cast(
                Sequence[tuple[str, str | None]],
                (
                    await session.execute(
                        select(ConversationRow.conversation_id, ConversationRow.forked_at).where(
                            col(ConversationRow.forked_at).is_not(None)
                        )
                    )
                ).all(),
            )
            ticket_rows = cast(
                Sequence[tuple[str, str | None]],
                (
                    await session.execute(
                        select(TicketRow.task_id, TicketRow.anchor_entry_id).where(
                            col(TicketRow.main_conversation_id) == conversation_id,
                            col(TicketRow.anchor_entry_id).is_not(None),
                        )
                    )
                ).all(),
            )

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = str(page_rows[-1]["parent_id"]) if has_more and page_rows and page_rows[-1]["parent_id"] else None

        children_by_entry: dict[str, list[str]] = {}
        for cid, fat in forked_rows:
            if fat is not None:
                children_by_entry.setdefault(str(fat), []).append(str(cid))
        tasks_by_entry: dict[str, list[str]] = {}
        for tid, aid in ticket_rows:
            if aid is not None:
                tasks_by_entry.setdefault(str(aid), []).append(str(tid))

        entries: list[EntryRecordData] = []
        for r in page_rows:
            eid = str(r["id"])
            raw_msg = r["message_json"]
            parts: list[EntryPartData] = []
            if isinstance(raw_msg, str):
                # OPT-6: 列表/分页读取绝不从磁盘水合大二进制载荷
                msg = await load_message_async(raw_msg, media_store=None, hydrate_media=False)
                parts = _message_to_part_data(msg)
            kind_raw = str(r["kind"])
            kind: Literal["root", "conv_init", "message", "handoff"] = (
                kind_raw
                if kind_raw in ("root", "conv_init", "message", "handoff")
                else "message"
            )
            entries.append(
                EntryRecordData(
                    entry_id=eid,
                    parent_id=str(r["parent_id"]) if r["parent_id"] is not None else None,
                    kind=kind,
                    stop_backtrack=bool(r["stop_backtrack"]),
                    run_id=str(r["run_id"]) if r["run_id"] is not None else None,
                    parts=parts,
                    child_branch_ids=children_by_entry.get(eid, []),
                    attached_task_ids=tasks_by_entry.get(eid, []),
                    created_at=str(r["created_at"]),
                )
            )
        entries.reverse()
        return EntryPageData(
            conversation_id=conversation_id,
            entries=entries,
            has_more=has_more,
            next_before_entry_id=next_cursor,
        )

    async def save_context_snapshot(
        self,
        *,
        conversation_id: str,
        run_id: str,
        model_name: str,
        window_used_tokens: int,
        window_limit_tokens: int | None,
        calibrated: bool,
        snapshot_json: str,
    ) -> None:
        now = utc_iso()
        async with self._sessions() as session, session.begin():
            row = await session.get(ContextSnapshotRow, conversation_id)
            if row is None:
                session.add(
                    ContextSnapshotRow(
                        conversation_id=conversation_id,
                        run_id=run_id,
                        model_name=model_name,
                        window_used_tokens=window_used_tokens,
                        window_limit_tokens=window_limit_tokens,
                        calibrated=1 if calibrated else 0,
                        snapshot_json=snapshot_json,
                        updated_at=now,
                    )
                )
            else:
                row.run_id = run_id
                row.model_name = model_name
                row.window_used_tokens = window_used_tokens
                row.window_limit_tokens = window_limit_tokens
                row.calibrated = 1 if calibrated else 0
                row.snapshot_json = snapshot_json
                row.updated_at = now

    async def get_context_snapshot(self, conversation_id: str) -> ContextSnapshotRow | None:
        async with self._sessions() as session:
            return await session.get(ContextSnapshotRow, conversation_id)

    async def record_aux_usage(
        self,
        *,
        conversation_id: str,
        source: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        now = utc_iso()
        async with self._sessions() as session, session.begin():
            row = await session.get(ConversationAuxUsageRow, (conversation_id, source))
            eff_total = total_tokens if total_tokens > 0 else (input_tokens + output_tokens)
            if row is None:
                session.add(
                    ConversationAuxUsageRow(
                        conversation_id=conversation_id,
                        source=source,
                        requests=1,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=eff_total,
                        cost_usd=cost_usd,
                        updated_at=now,
                    )
                )
            else:
                row.requests += 1
                row.input_tokens += input_tokens
                row.output_tokens += output_tokens
                row.total_tokens += eff_total
                row.cost_usd += cost_usd
                row.updated_at = now

    async def list_aux_usage(self, conversation_id: str) -> list[ConversationAuxUsageRow]:
        async with self._sessions() as session:
            stmt = select(ConversationAuxUsageRow).where(
                col(ConversationAuxUsageRow.conversation_id) == conversation_id
            )
            return list((await session.execute(stmt)).scalars().all())
