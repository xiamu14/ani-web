"""数据库包: 对外暴露 SQLite 连接、SQLModel 模型、ConversationStore 与 TicketStore 实现。"""

from agent.db.conversation_store import (
    DEFAULT_TREE_ID,
    LAST_ACTIVE_CONV_KEY,
    ROOT_ENTRY_KEY,
    SQLModelConversationStore,
)
from agent.db.engine import create_session_factory, create_sqlite_async_engine, init_async_db
from agent.db.mcp_policy_store import SQLModelMcpPolicyStore
from agent.db.models import (
    AppStateRow,
    ContextEntryRow,
    ConversationRow,
    McpToolPolicyRow,
    TicketRow,
)
from agent.db.schema import SCHEMA_SQL, SCHEMA_VERSION, init_schema
from agent.db.sqlite import connect_sqlite, default_db_path, open_database
from agent.db.ticket_store import SQLModelTicketStore, parse_depends_on, paths_overlap

__all__ = [
    "AppStateRow",
    "ContextEntryRow",
    "ConversationRow",
    "DEFAULT_TREE_ID",
    "LAST_ACTIVE_CONV_KEY",
    "McpToolPolicyRow",
    "ROOT_ENTRY_KEY",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "SQLModelConversationStore",
    "SQLModelMcpPolicyStore",
    "SQLModelTicketStore",
    "TicketRow",
    "connect_sqlite",
    "create_session_factory",
    "create_sqlite_async_engine",
    "default_db_path",
    "init_async_db",
    "init_schema",
    "open_database",
    "parse_depends_on",
    "paths_overlap",
]
