"""SQLModel 数据表模型，对应 SQLite 本地数据库 Schema。"""

from __future__ import annotations

from typing import ClassVar

from sqlmodel import Field, SQLModel


class ContextEntryRow(SQLModel, table=True):
    """上下文树节点表（context_entries）ORM 模型。"""

    __tablename__: ClassVar[str] = "context_entries"  # pyright: ignore[reportIncompatibleVariableOverride]

    id: str = Field(primary_key=True)
    tree_id: str = Field(index=True)
    parent_id: str | None = Field(default=None, foreign_key="context_entries.id", index=True)
    kind: str = Field(default="message")
    stop_backtrack: int = Field(default=0)
    message_json: str | None = Field(default=None)
    run_id: str | None = Field(default=None)
    created_at: str


class ConversationRow(SQLModel, table=True):
    """主对话与任务级 Role 子会话游标表（conversations）ORM 模型。"""

    __tablename__: ClassVar[str] = "conversations"  # pyright: ignore[reportIncompatibleVariableOverride]

    conversation_id: str = Field(primary_key=True)
    tree_id: str = Field(index=True)
    parent_conversation_id: str | None = Field(
        default=None, foreign_key="conversations.conversation_id", index=True
    )
    role_name: str | None = Field(default=None)
    scope: str | None = Field(default=None)
    head_entry_id: str | None = Field(default=None, foreign_key="context_entries.id")
    forked_from: str | None = Field(default=None, foreign_key="conversations.conversation_id")
    forked_at: str | None = Field(default=None, foreign_key="context_entries.id")
    branch_mode: str = Field(default="root")
    branches_json: str = Field(default="[]")
    work_path: str | None = Field(default=None)
    target_title: str = Field(default="")
    status: str = Field(default="idle")
    usage_json: str = Field(default="{}")
    deferred_json: str | None = Field(default=None)
    revision: str
    updated_at: str


class TicketRow(SQLModel, table=True):
    """异步任务单与目录锁排队账本表（tickets）ORM 模型。"""

    __tablename__: ClassVar[str] = "tickets"  # pyright: ignore[reportIncompatibleVariableOverride]

    main_conversation_id: str = Field(primary_key=True, foreign_key="conversations.conversation_id")
    task_id: str = Field(primary_key=True)
    role_conversation_id: str | None = Field(
        default=None, foreign_key="conversations.conversation_id", index=True
    )
    role_name: str
    anchor_entry_id: str | None = Field(default=None, foreign_key="context_entries.id")
    work_path: str | None = Field(default=None)
    lock_mode: str = Field(default="exclusive")
    depends_on_json: str = Field(default="[]")
    mandate: str
    status: str = Field(default="queued")
    run_id: str | None = Field(default=None)
    result_summary: str | None = Field(default=None)
    created_at: str
    updated_at: str


class AppStateRow(SQLModel, table=True):
    """全局元祖指针与重启恢复指针表（app_state）ORM 模型。"""

    __tablename__: ClassVar[str] = "app_state"  # pyright: ignore[reportIncompatibleVariableOverride]

    key: str = Field(primary_key=True)
    value: str
    updated_at: str


class McpToolPolicyRow(SQLModel, table=True):
    """MCP 工具增量预编译权限与队列策略表（mcp_tool_policies）ORM 模型。"""

    __tablename__: ClassVar[str] = "mcp_tool_policies"  # pyright: ignore[reportIncompatibleVariableOverride]

    server_name: str = Field(primary_key=True)
    tool_name: str = Field(primary_key=True)
    endpoint_fingerprint: str
    server_impl_name: str
    server_version: str
    prefixed_tool_name: str = Field(index=True)
    schema_hash: str
    level: str
    lock_mode: str
    recommended_roles_json: str = Field(default="[]")
    reason: str = Field(default="")
    updated_at: str


class ObservationRow(SQLModel, table=True):
    """原子记忆观察表（observations）ORM 模型。"""

    __tablename__: ClassVar[str] = "observations"  # pyright: ignore[reportIncompatibleVariableOverride]

    id: int | None = Field(default=None, primary_key=True)
    project_key: str = Field(index=True)
    conversation_id: str
    role_name: str
    type: str
    title: str
    subtitle: str = Field(default="")
    facts_json: str = Field(default="[]")
    narrative: str = Field(default="")
    concepts_json: str = Field(default="[]")
    files_read_json: str = Field(default="[]")
    files_modified_json: str = Field(default="[]")
    line_anchors_json: str = Field(default="[]")
    file_hashes_json: str = Field(default="{}")
    provenance: str = Field(default="verified_by_tool")
    discovery_tokens: int = Field(default=0)
    reinforcement_dates_json: str = Field(default="[]")
    superseded_by: int | None = Field(default=None, foreign_key="observations.id")
    created_at_epoch: int


class SessionSummaryRow(SQLModel, table=True):
    """会话/Role 任务阶段总结表（session_summaries）ORM 模型。"""

    __tablename__: ClassVar[str] = "session_summaries"  # pyright: ignore[reportIncompatibleVariableOverride]

    id: int | None = Field(default=None, primary_key=True)
    project_key: str = Field(index=True)
    conversation_id: str
    task_id: str = Field(default="")
    request: str = Field(default="")
    investigated: str = Field(default="")
    learned: str = Field(default="")
    completed: str = Field(default="")
    next_steps: str = Field(default="")
    notes: str = Field(default="")
    files_read_json: str = Field(default="[]")
    files_edited_json: str = Field(default="[]")
    discovery_tokens: int = Field(default=0)
    created_at_epoch: int


class WorkStateRow(SQLModel, table=True):
    """开放工作状态账本表（work_states）ORM 模型。"""

    __tablename__: ClassVar[str] = "work_states"  # pyright: ignore[reportIncompatibleVariableOverride]

    id: int | None = Field(default=None, primary_key=True)
    project_key: str = Field(index=True)
    list_name: str
    task_name: str
    fields_json: str = Field(default="{}")
    status: str = Field(default="open")
    created_at_epoch: int
