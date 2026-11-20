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

CREATE TABLE IF NOT EXISTS mcp_tool_policies (
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    endpoint_fingerprint TEXT NOT NULL,
    server_impl_name TEXT NOT NULL,
    server_version TEXT NOT NULL,
    prefixed_tool_name TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('pass', 'review', 'forbid')),
    lock_mode TEXT NOT NULL CHECK (lock_mode IN ('shared', 'exclusive', 'none')),
    recommended_roles_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (server_name, tool_name)
);

CREATE INDEX IF NOT EXISTS idx_mcp_tool_policies_prefixed
    ON mcp_tool_policies(prefixed_tool_name);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('decision', 'bugfix', 'feature', 'refactor', 'discovery', 'change')),
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    facts_json TEXT NOT NULL DEFAULT '[]',
    narrative TEXT NOT NULL DEFAULT '',
    concepts_json TEXT NOT NULL DEFAULT '[]',
    files_read_json TEXT NOT NULL DEFAULT '[]',
    files_modified_json TEXT NOT NULL DEFAULT '[]',
    line_anchors_json TEXT NOT NULL DEFAULT '[]',
    file_hashes_json TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT 'verified_by_tool'
        CHECK (provenance IN ('verified_by_tool', 'user_explicit', 'agent_inferred')),
    discovery_tokens INTEGER NOT NULL DEFAULT 0,
    reinforcement_dates_json TEXT NOT NULL DEFAULT '[]',
    superseded_by INTEGER REFERENCES observations(id),
    created_at_epoch INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_project_epoch
    ON observations(project_key, created_at_epoch DESC, id DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    title,
    subtitle,
    facts_json,
    narrative,
    concepts_json,
    files_read_json,
    files_modified_json,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(
        rowid, title, subtitle, facts_json, narrative, concepts_json, files_read_json, files_modified_json
    ) VALUES (
        new.id, new.title, new.subtitle, new.facts_json, new.narrative, new.concepts_json, new.files_read_json, new.files_modified_json
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_observations_ad AFTER DELETE ON observations BEGIN
    DELETE FROM observations_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_observations_au AFTER UPDATE ON observations BEGIN
    DELETE FROM observations_fts WHERE rowid = old.id;
    INSERT INTO observations_fts(
        rowid, title, subtitle, facts_json, narrative, concepts_json, files_read_json, files_modified_json
    ) VALUES (
        new.id, new.title, new.subtitle, new.facts_json, new.narrative, new.concepts_json, new.files_read_json, new.files_modified_json
    );
END;

CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    request TEXT NOT NULL DEFAULT '',
    investigated TEXT NOT NULL DEFAULT '',
    learned TEXT NOT NULL DEFAULT '',
    completed TEXT NOT NULL DEFAULT '',
    next_steps TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    files_read_json TEXT NOT NULL DEFAULT '[]',
    files_edited_json TEXT NOT NULL DEFAULT '[]',
    discovery_tokens INTEGER NOT NULL DEFAULT 0,
    created_at_epoch INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_summaries_project_epoch
    ON session_summaries(project_key, created_at_epoch DESC, id DESC);

CREATE TABLE IF NOT EXISTS work_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key TEXT NOT NULL,
    list_name TEXT NOT NULL,
    task_name TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'blocked', 'done', 'cancelled')),
    created_at_epoch INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_states_project_epoch
    ON work_states(project_key, created_at_epoch ASC, id ASC);

CREATE TABLE IF NOT EXISTS context_snapshots (
    conversation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    window_used_tokens INTEGER NOT NULL DEFAULT 0,
    window_limit_tokens INTEGER,
    calibrated INTEGER NOT NULL DEFAULT 0 CHECK (calibrated IN (0, 1)),
    snapshot_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_aux_usage (
    conversation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, source)
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """在当前连接上幂等创建全部表与索引，并写入 user_version。"""
    with conn:
        _ = conn.executescript(SCHEMA_SQL)
        _ = conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
