"""SQLite 本地数据库 Schema 定义与初始化。"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS context_entries (
    id TEXT PRIMARY KEY,
    tree_id TEXT NOT NULL,
    parent_id TEXT REFERENCES context_entries(id),
    kind TEXT NOT NULL CHECK (kind IN ('root', 'conv_init', 'message', 'handoff')),
    stop_backtrack INTEGER NOT NULL DEFAULT 0 CHECK (stop_backtrack IN (0, 1)),
    message_json TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_entries_tree_id
    ON context_entries(tree_id);

CREATE INDEX IF NOT EXISTS idx_context_entries_parent_id
    ON context_entries(parent_id);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    tree_id TEXT NOT NULL,
    parent_conversation_id TEXT REFERENCES conversations(conversation_id),
    role_name TEXT,
    scope TEXT,
    head_entry_id TEXT REFERENCES context_entries(id),
    forked_from TEXT REFERENCES conversations(conversation_id),
    forked_at TEXT REFERENCES context_entries(id),
    branch_mode TEXT NOT NULL DEFAULT 'root'
        CHECK (branch_mode IN ('root', 'root_only', 'handoff', 'full_fork')),
    branches_json TEXT NOT NULL DEFAULT '[]',
    work_path TEXT,
    target_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle'
        CHECK (status IN ('idle', 'running', 'waiting_deferred', 'failed', 'interrupted')),
    usage_json TEXT NOT NULL DEFAULT '{}',
    deferred_json TEXT,
    revision TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_tree_id
    ON conversations(tree_id);

CREATE INDEX IF NOT EXISTS idx_conversations_parent_conv_id
    ON conversations(parent_conversation_id);

CREATE INDEX IF NOT EXISTS idx_conversations_scope_updated
    ON conversations(scope, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    base_entry_id TEXT REFERENCES context_entries(id),
    state TEXT NOT NULL CHECK (state IN ('running', 'complete', 'interrupted', 'failed')),
    frontier_messages_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation_id
    ON agent_runs(conversation_id);

CREATE TABLE IF NOT EXISTS tickets (
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

CREATE INDEX IF NOT EXISTS idx_tickets_status_created
    ON tickets(main_conversation_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_tickets_role_conv_id
    ON tickets(role_conversation_id);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """在当前连接上幂等创建全部表与索引，并写入 user_version。"""
    with conn:
        _ = conn.executescript(SCHEMA_SQL)
        _ = conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
