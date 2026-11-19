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


async def init_async_db(engine: AsyncEngine) -> None:
    """在异步引擎上幂等初始化全部表结构与索引。"""
    async with engine.begin() as conn:
        raw_conn = await conn.get_raw_connection()
        driver_conn = raw_conn.driver_connection
        if driver_conn is None:
            raise RuntimeError("未能获取底层 aiosqlite 连接以执行 Schema 初始化。")
        await driver_conn.executescript(SCHEMA_SQL)
        await driver_conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
