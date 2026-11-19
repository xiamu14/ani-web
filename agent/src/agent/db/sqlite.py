"""SQLite 本地数据库连接管理与固定路径定位。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from agent.db.schema import init_schema

_DB_ENV_VAR = "AGENT_DB_PATH"


def default_db_path() -> Path:
    """返回固定的会话数据库路径（优先读取 AGENT_DB_PATH，不随工作目录变化）。"""
    configured = os.environ.get(_DB_ENV_VAR)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".agent" / "context_tree.db"


def connect_sqlite(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开本地 SQLite 连接并配置外键、WAL 与超时参数。"""
    if db_path == ":memory:":
        target = ":memory:"
        is_memory = True
    else:
        resolved = Path(db_path).expanduser().resolve() if db_path is not None else default_db_path()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        target = str(resolved)
        is_memory = False

    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def open_database(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开本地 SQLite 数据库并确保 Schema 初始化完成。"""
    conn = connect_sqlite(db_path)
    init_schema(conn)
    return conn
