"""数据库包: 对外暴露 SQLite 连接、SQLModel 模型、ConversationStore 与 TicketStore 实现。"""

from agent.db.conversation_store import (
    DEFAULT_TREE_ID,
    LAST_ACTIVE_CONV_KEY,
    ROOT_ENTRY_KEY,
    SQLModelConversationStore,
)
from agent.db.engine import (
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)
from agent.db.mcp_policy_store import SQLModelMcpPolicyStore
from agent.db.media_store import (
    DEFAULT_MEDIA_THRESHOLD_BYTES,
    DEFAULT_MEDIA_WINDOW_MESSAGES,
    CompressedFileMediaStore,
    externalize_message_tree,
    replace_external_markers_with_stubs,
    restore_message_tree,
)
from agent.db.memory_store import SQLModelMemoryStore
from agent.db.models import (
    AppStateRow,
    ContextEntryRow,
    ContextSnapshotRow,
    ConversationAuxUsageRow,
    ConversationRow,
    McpToolPolicyRow,
    ObservationRow,
    SessionSummaryRow,
    TicketRow,
    WorkStateRow,
)
from agent.db.schema import SCHEMA_SQL, SCHEMA_VERSION, init_schema
from agent.db.sqlite import connect_sqlite, default_db_path, open_database
from agent.db.ticket_store import SQLModelTicketStore, parse_depends_on, paths_overlap
from agent.db.tree_queries import (
    EntryPageData,
    EntryPartData,
    EntryRecordData,
    SQLModelProtocolStore,
    TreeBranchRecord,
    TreeTopologyData,
)

__all__ = [
    "DEFAULT_MEDIA_THRESHOLD_BYTES",
    "DEFAULT_MEDIA_WINDOW_MESSAGES",
    "DEFAULT_TREE_ID",
    "LAST_ACTIVE_CONV_KEY",
    "ROOT_ENTRY_KEY",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "AppStateRow",
    "CompressedFileMediaStore",
    "ContextEntryRow",
    "ContextSnapshotRow",
    "ConversationAuxUsageRow",
    "ConversationRow",
    "EntryPageData",
    "EntryPartData",
    "EntryRecordData",
    "McpToolPolicyRow",
    "ObservationRow",
    "SQLModelConversationStore",
    "SQLModelMcpPolicyStore",
    "SQLModelMemoryStore",
    "SQLModelProtocolStore",
    "SQLModelTicketStore",
    "SessionSummaryRow",
    "TicketRow",
    "TreeBranchRecord",
    "TreeTopologyData",
    "WorkStateRow",
    "connect_sqlite",
    "create_session_factory",
    "create_sqlite_async_engine",
    "default_db_path",
    "externalize_message_tree",
    "init_async_db",
    "init_schema",
    "open_database",
    "parse_depends_on",
    "paths_overlap",
    "replace_external_markers_with_stubs",
    "restore_message_tree",
]
