"""SQLModel + aiosqlite 异步数据库引擎与会话工厂。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent.db.schema import SCHEMA_SQL, SCHEMA_VERSION
from agent.db.sqlite import default_db_path


def _sqlite_url(db_path: str | Path | None) -> tuple[str, bool]:
    if db_path == ":memory:":
        return "sqlite+aiosqlite:///:memory:", True
    resolved = (
        Path(db_path).expanduser().resolve()
        if db_path is not None
        else default_db_path()
    )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{resolved}", False


def create_sqlite_async_engine(db_path: str | Path | None = None) -> AsyncEngine:
    """创建配置好外键、超时与 WAL 模式的异步 SQLite 引擎。"""
    url, is_memory = _sqlite_url(db_path)
    engine = create_async_engine(url, echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_pragma(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        cursor = dbapi_conn.cursor()
        try:
            _ = cursor.execute("PRAGMA foreign_keys = ON")
            _ = cursor.execute("PRAGMA busy_timeout = 5000")
            if not is_memory:
                _ = cursor.execute("PRAGMA journal_mode = WAL")
                _ = cursor.execute("PRAGMA synchronous = NORMAL")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建 expire_on_commit=False 的异步会话工厂。"""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("tickets", "depends_on_json", "TEXT NOT NULL DEFAULT '[]'"),
)


_TICKETS_REBUILD_SQL = """
PRAGMA foreign_keys = OFF;
CREATE TABLE tickets_new (
    main_conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    task_id TEXT NOT NULL,
    role_conversation_id TEXT REFERENCES conversations(conversation_id),
    role_name TEXT NOT NULL,
    anchor_entry_id TEXT REFERENCES context_entries(id),
    work_path TEXT,
    lock_mode TEXT NOT NULL DEFAULT 'exclusive'
        CHECK (lock_mode IN ('exclusive', 'shared', 'none')),
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    mandate TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')),
    run_id TEXT,
    result_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (main_conversation_id, task_id)
);
INSERT INTO tickets_new (
    main_conversation_id, task_id, role_conversation_id, role_name,
    anchor_entry_id, work_path, lock_mode, depends_on_json, mandate,
    status, run_id, result_summary, created_at, updated_at
)
SELECT
    main_conversation_id, task_id, role_conversation_id, role_name,
    anchor_entry_id, work_path, lock_mode, depends_on_json, mandate,
    status, run_id, result_summary, created_at, updated_at
FROM tickets;
DROP TABLE tickets;
ALTER TABLE tickets_new RENAME TO tickets;
CREATE INDEX IF NOT EXISTS idx_tickets_status_created
    ON tickets(main_conversation_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_role_conv_id
    ON tickets(role_conversation_id);
PRAGMA foreign_keys = ON;
"""


async def init_async_db(engine: AsyncEngine) -> None:
    """在异步引擎上幂等初始化全部表结构与索引。"""
    async with engine.begin() as conn:
        raw_conn = await conn.get_raw_connection()
        driver_conn = raw_conn.driver_connection
        if driver_conn is None:
            raise RuntimeError("未能获取底层 aiosqlite 连接以执行 Schema 初始化。")
        await driver_conn.executescript(SCHEMA_SQL)
        for table, col, ddl in _COLUMN_MIGRATIONS:
            cursor = await driver_conn.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            existing = {str(r[1]) for r in rows}
            if col not in existing:
                _ = await driver_conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"
                )
        cur = await driver_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tickets'"
        )
        t_row = await cur.fetchone()
        if t_row and t_row[0] and "'shared'" not in str(t_row[0]):
            await driver_conn.executescript(_TICKETS_REBUILD_SQL)
        await driver_conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
