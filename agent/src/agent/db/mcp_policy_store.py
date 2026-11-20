"""基于 SQLModel + aiosqlite 的 MCP 预编译策略仓库（实现 McpPolicyStorePort）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlmodel import col, select

from agent.db.engine import create_session_factory
from agent.db.models import McpToolPolicyRow
from agent.permissions.mcp_ports import LockMode, McpToolPolicy
from agent.permissions.rules import RuleLevel


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_roles(raw_json: str) -> tuple[str, ...]:
    try:
        parsed: object = json.loads(raw_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item).strip() for item in parsed if str(item).strip())


def _row_to_policy(row: McpToolPolicyRow) -> McpToolPolicy:
    return McpToolPolicy(
        server_name=row.server_name,
        endpoint_fingerprint=row.endpoint_fingerprint,
        server_impl_name=row.server_impl_name,
        server_version=row.server_version,
        tool_name=row.tool_name,
        prefixed_tool_name=row.prefixed_tool_name,
        schema_hash=row.schema_hash,
        level=cast(RuleLevel, row.level),
        lock_mode=cast(LockMode, row.lock_mode),
        recommended_roles=_parse_roles(row.recommended_roles_json),
        reason=row.reason,
    )


class SQLModelMcpPolicyStore:
    """持久化管理 `mcp_tool_policies` 表的异步仓储实现。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine: AsyncEngine = engine
        self._sessions: async_sessionmaker[AsyncSession] = session_factory or create_session_factory(engine)

    async def list_by_server(self, server_name: str) -> list[McpToolPolicy]:
        async with self._sessions() as session:
            stmt = select(McpToolPolicyRow).where(col(McpToolPolicyRow.server_name) == server_name)
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_policy(row) for row in rows]

    async def list_all(self) -> list[McpToolPolicy]:
        async with self._sessions() as session:
            stmt = select(McpToolPolicyRow).order_by(
                col(McpToolPolicyRow.server_name),
                col(McpToolPolicyRow.tool_name),
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_policy(row) for row in rows]

    async def replace_server_policies(
        self,
        server_name: str,
        policies: Sequence[McpToolPolicy],
    ) -> None:
        now = _utc_iso()
        async with self._sessions() as session:
            await session.execute(
                delete(McpToolPolicyRow).where(col(McpToolPolicyRow.server_name) == server_name)
            )
            for policy in policies:
                session.add(
                    McpToolPolicyRow(
                        server_name=policy.server_name,
                        tool_name=policy.tool_name,
                        endpoint_fingerprint=policy.endpoint_fingerprint,
                        server_impl_name=policy.server_impl_name,
                        server_version=policy.server_version,
                        prefixed_tool_name=policy.prefixed_tool_name,
                        schema_hash=policy.schema_hash,
                        level=policy.level,
                        lock_mode=policy.lock_mode,
                        recommended_roles_json=json.dumps(list(policy.recommended_roles), ensure_ascii=False),
                        reason=policy.reason,
                        updated_at=now,
                    )
                )
            await session.commit()
