"""ConversationStore 内部序列化与节点清理辅助函数。"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import TypeAdapter
from pydantic_ai._deferred import DeferredToolRequests
from pydantic_ai.conversation_store import ConversationRecord
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness._history import STORED_HISTORY_CONTEXT
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import ConversationRow

USAGE_ADAPTER = TypeAdapter(RunUsage)
DEFERRED_ADAPTER = TypeAdapter(DeferredToolRequests)
BRANCHES_ADAPTER = TypeAdapter(list[str])


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(UTC)).isoformat()


def dump_message(message: ModelMessage) -> str:
    return ModelMessagesTypeAdapter.dump_json([message]).decode("utf-8")


def load_message(raw: str) -> ModelMessage:
    return ModelMessagesTypeAdapter.validate_json(raw, context=STORED_HISTORY_CONTEXT)[0]


def row_to_record(row: ConversationRow) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=row.conversation_id,
        head_id=row.head_entry_id,
        usage=USAGE_ADAPTER.validate_json(row.usage_json),
        deferred_tool_requests=(
            DEFERRED_ADAPTER.validate_json(row.deferred_json)
            if row.deferred_json is not None
            else None
        ),
        scope=row.scope,
        forked_from=row.forked_from,
        forked_at=row.forked_at,
        branches=BRANCHES_ADAPTER.validate_json(row.branches_json),
        revision=row.revision,
        updated_at=datetime.fromisoformat(row.updated_at),
    )


def derive_parent_conv_id(scope: str | None) -> str | None:
    if scope is not None and scope.startswith("conv:"):
        return scope.removeprefix("conv:")
    return None


async def gc_unreachable_entries(session: AsyncSession, *, tree_id: str) -> None:
    """使用单条 SQL WITH RECURSIVE 清理不再被任何剩余分支引用的非 root 节点。"""
    sql = text("""
        WITH RECURSIVE
            tips(id) AS (
                SELECT head_entry_id
                FROM conversations
                WHERE tree_id = :tree_id AND head_entry_id IS NOT NULL
                UNION
                SELECT je.value
                FROM conversations c, json_each(c.branches_json) AS je
                WHERE c.tree_id = :tree_id
            ),
            reachable(id) AS (
                SELECT id FROM tips
                UNION
                SELECT e.parent_id
                FROM context_entries AS e
                JOIN reachable AS r ON e.id = r.id
                WHERE e.parent_id IS NOT NULL
            )
        DELETE FROM context_entries
        WHERE tree_id = :tree_id
          AND kind != 'root'
          AND id NOT IN (SELECT id FROM reachable)
    """)
    _ = await session.execute(sql, {"tree_id": tree_id})
