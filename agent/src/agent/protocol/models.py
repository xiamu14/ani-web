"""Layer 0 GUI 通信协议契约模型（Task、Interaction、Tree、Chat 与 Snapshot 视图）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from agent.protocol.models_base import (
    PROTOCOL_VERSION,
    ArtifactView,
    AttachmentKindLiteral,
    AttachmentView,
    AuxJevUsageView,
    BudgetProgressView,
    CitationLocatorView,
    ContextBucketCategory,
    ContextBucketView,
    ContextSavingsView,
    ContextSubItemView,
    ContextWindowSnapshotView,
    ErrorEnvelope,
    LlmPoolStatusView,
    LocalAttachmentRequest,
    PoolStatsView,
    RecoveredBranchView,
    RoleTaskUsageItemView,
    RunUsageView,
    StrictProtocolModel,
    SuggestedAction,
    TreeUsageSummaryView,
    VerifiedCitationView,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ActiveToolView",
    "ApprovalContextView",
    "ApprovalItemView",
    "ApprovalPayloadView",
    "ApprovalRespondPayload",
    "ArtifactView",
    "AskUserAnswerItemModel",
    "AskUserOptionView",
    "AskUserPayloadView",
    "AskUserQuestionView",
    "AskUserRespondPayload",
    "AttachmentKindLiteral",
    "AttachmentView",
    "AuxJevUsageView",
    "BranchModeLiteral",
    "BranchView",
    "BudgetProgressView",
    "CitationLocatorView",
    "ContextBucketCategory",
    "ContextBucketView",
    "ContextSavingsView",
    "ContextSubItemView",
    "ContextWindowSnapshotView",
    "CreateConversationRequest",
    "EntryPageView",
    "EntryPartView",
    "EntryView",
    "ErrorEnvelope",
    "ForkConversationRequest",
    "HealthView",
    "InteractionKindLiteral",
    "InteractionRespondRequest",
    "InteractionView",
    "InterruptResponse",
    "ItemApprovalDecisionModel",
    "LlmPoolStatusView",
    "LocalAttachmentRequest",
    "McpElicitationPayloadView",
    "McpElicitationRespondPayload",
    "PatchConversationRequest",
    "PoolStatsView",
    "RecoveredBranchView",
    "RoleTaskUsageItemView",
    "RunUsageView",
    "RuntimeSnapshotView",
    "SendMessageReceipt",
    "SendMessageRequest",
    "StrictProtocolModel",
    "SubscriptionUpdateRequest",
    "SuggestedAction",
    "TaskCancelRequest",
    "TaskLivePhaseLiteral",
    "TaskStatusLiteral",
    "TaskSteerRequest",
    "TaskView",
    "ToolStepSummaryView",
    "TreeUsageSummaryView",
    "TreeView",
    "VerifiedCitationView",
    "WorkspaceLockView",
]


# ---------------------------------------------------------------------------
# Role Tasks & Workspace Locks
# ---------------------------------------------------------------------------


TaskStatusLiteral = Literal[
    "queued", "running", "completed", "failed", "cancelled", "interrupted"
]
TaskLivePhaseLiteral = Literal[
    "waiting_dependency",
    "acquiring_lock",
    "thinking",
    "calling_tool",
    "waiting_approval",
    "waiting_user",
    "retrying_rate_limit",
    "wrapping_up",
    "settled",
]


class ActiveToolView(StrictProtocolModel):
    """当前正在执行的工具状态。"""

    tool_call_id: str
    tool_name: str
    args_preview: str = ""
    started_at: str


class ToolStepSummaryView(StrictProtocolModel):
    """最近工具执行步摘要。"""

    tool_name: str
    succeeded: bool
    duration_ms: int = 0
    error_preview: str = ""


class TaskView(StrictProtocolModel):
    """后台 Role Agent 任务实时看板视图。"""

    main_conversation_id: str
    task_id: str
    role_name: str
    role_conversation_id: str | None = None
    mandate: str
    work_path: str | None = None
    lock_mode: Literal["exclusive", "shared", "none"] = "exclusive"
    status: TaskStatusLiteral
    live_phase: TaskLivePhaseLiteral = "settled"
    queue_position: int = 0
    depends_on: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    pending_interaction_ids: list[str] = Field(default_factory=list)
    current_step: int = 0
    max_requests: int = 35
    tool_calls_count: int = 0
    max_tool_calls: int = 80
    soft_limit_triggered: bool = False
    active_tool: ActiveToolView | None = None
    recent_steps: list[ToolStepSummaryView] = Field(default_factory=list)
    result_summary: str | None = None
    usage: RunUsageView = Field(default_factory=RunUsageView)
    created_at: str
    updated_at: str


class WorkspaceLockView(StrictProtocolModel):
    """工作区读写锁实时状态。"""

    work_path: str
    exclusive_holder: str | None = None
    shared_holders: list[str] = Field(default_factory=list)
    queued_tasks: list[str] = Field(default_factory=list)


class TaskSteerRequest(StrictProtocolModel):
    """针对 Role Task 的 Steer / Revise 请求。"""

    instruction: str


class TaskCancelRequest(StrictProtocolModel):
    """取消 Role Task 请求。"""

    reason: str = ""


# ---------------------------------------------------------------------------
# Interactions (Per-Item Approval / AskUser / MCP Elicitation)
# ---------------------------------------------------------------------------


InteractionKindLiteral = Literal["approval", "ask_user", "mcp_elicitation"]


class ApprovalItemView(StrictProtocolModel):
    """待逐条勾选审核的单个工具调用描述。"""

    item_id: str
    tool_name: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    category: str
    rule_id: str
    reason: str
    risk_level: Literal["low", "medium", "high"] = "medium"


class ApprovalContextView(StrictProtocolModel):
    """审批请求所属会话与任务上下文。"""

    main_conversation_id: str
    task_id: str | None = None
    role_name: str | None = None
    run_id: str
    user_intent: str = ""


class ApprovalPayloadView(StrictProtocolModel):
    """权限审核交互详情。"""

    items: list[ApprovalItemView]
    context: ApprovalContextView


class AskUserOptionView(StrictProtocolModel):
    """AskUser 单个可选项。"""

    label: str
    description: str | None = None


class AskUserQuestionView(StrictProtocolModel):
    """AskUser 单个问题。"""

    header: str
    question: str
    options: list[AskUserOptionView]
    multi_select: bool = False


class AskUserPayloadView(StrictProtocolModel):
    """LLM 选择建议问答交互详情。"""

    request_id: str
    questions: list[AskUserQuestionView]


class McpElicitationPayloadView(StrictProtocolModel):
    """MCP Server 发起的交互式表单请求。"""

    server_name: str
    message: str
    requested_schema: dict[str, JsonValue] = Field(default_factory=dict)


class InteractionView(StrictProtocolModel):
    """统一交互挂起卡片视图。"""

    interaction_id: str
    kind: InteractionKindLiteral
    status: Literal["pending", "resolved", "cancelled"] = "pending"
    conversation_id: str
    task_id: str | None = None
    role_name: str | None = None
    run_id: str | None = None
    created_at: str
    approval: ApprovalPayloadView | None = None
    ask_user: AskUserPayloadView | None = None
    mcp_elicitation: McpElicitationPayloadView | None = None


class ItemApprovalDecisionModel(StrictProtocolModel):
    """单条工具调用的逐条勾选裁决。"""

    approved: bool
    grant_category_for_run: bool = False
    reason: str = ""


class ApprovalRespondPayload(StrictProtocolModel):
    """提交权限审核结果（支持逐条 item_decisions 或向后兼容整批裁决）。"""

    kind: Literal["approval"] = "approval"
    item_decisions: dict[str, ItemApprovalDecisionModel] = Field(default_factory=dict)
    approved: bool | None = None
    grant_categories_for_run: list[str] = Field(default_factory=list)
    reason: str = ""


class AskUserAnswerItemModel(StrictProtocolModel):
    """提交 AskUser 单题回答。"""

    header: str
    selected: list[str] = Field(default_factory=list)
    custom_answer: str | None = None
    note_text: str = ""
    attachment_ids: list[str] = Field(default_factory=list)


class AskUserRespondPayload(StrictProtocolModel):
    """提交 AskUser 完整回答。"""

    kind: Literal["ask_user"] = "ask_user"
    cancelled: bool = False
    answers: list[AskUserAnswerItemModel] = Field(default_factory=list)


class McpElicitationRespondPayload(StrictProtocolModel):
    """提交 MCP Elicitation 表单结果。"""

    kind: Literal["mcp_elicitation"] = "mcp_elicitation"
    action: Literal["accept", "decline", "cancel"]
    content: dict[str, JsonValue] = Field(default_factory=dict)


InteractionRespondRequest = Annotated[
    ApprovalRespondPayload | AskUserRespondPayload | McpElicitationRespondPayload,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Conversation Tree & Chat Commands
# ---------------------------------------------------------------------------


BranchModeLiteral = Literal["root", "root_only", "handoff", "full_fork"]


class BranchView(StrictProtocolModel):
    """Level 1 宏观分支树节点视图。"""

    conversation_id: str
    scope: str
    parent_conversation_id: str | None = None
    role_name: str | None = None
    task_id: str | None = None
    target_title: str = ""
    work_path: str | None = None
    branch_mode: BranchModeLiteral = "root_only"
    forked_from: str | None = None
    forked_at: str | None = None
    head_entry_id: str | None = None
    status: str = "idle"
    is_running: bool = False
    message_count: int = 0
    active_task_count: int = 0
    pending_interaction_count: int = 0
    usage_summary: RunUsageView = Field(default_factory=RunUsageView)
    updated_at: str


class TreeView(StrictProtocolModel):
    """整棵会话树元数据拓扑。"""

    root_entry_id: str | None = None
    active_conversation_id: str | None = None
    branches: list[BranchView] = Field(default_factory=list)


class EntryPartView(StrictProtocolModel):
    """Level 2 消息节点内的轻量 Part 视图（大媒体仅传引用）。"""

    part_kind: str
    role: Literal["system", "user", "assistant", "tool"]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    attachment_id: str | None = None
    content_url: str | None = None


class EntryView(StrictProtocolModel):
    """Level 2 微观 ContextEntry DAG 节点。"""

    entry_id: str
    parent_id: str | None = None
    kind: Literal["root", "conv_init", "message", "handoff"]
    stop_backtrack: bool = False
    run_id: str | None = None
    parts: list[EntryPartView] = Field(default_factory=list)
    child_branch_ids: list[str] = Field(default_factory=list)
    attached_task_ids: list[str] = Field(default_factory=list)
    created_at: str


class EntryPageView(StrictProtocolModel):
    """分页 Entry DAG 响应。"""

    conversation_id: str
    entries: list[EntryView] = Field(default_factory=list)
    has_more: bool = False
    next_before_entry_id: str | None = None


class CreateConversationRequest(StrictProtocolModel):
    """从 Root 创建新主分支。"""

    target_title: str = ""
    work_path: str | None = None
    switch_to: bool = True


class ForkConversationRequest(StrictProtocolModel):
    """派生 full_fork 或 handoff 分支。"""

    mode: Literal["full_fork", "handoff"] = "full_fork"
    at_entry_id: str | None = None
    summary: str = ""
    target_title: str = ""
    work_path: str | None = None
    switch_to: bool = True


class PatchConversationRequest(StrictProtocolModel):
    """更新分支标题或绑定工作区。"""

    target_title: str | None = None
    work_path: str | None = None


class SendMessageRequest(StrictProtocolModel):
    """向会话发送消息、附件与投递控制指令。"""

    client_message_id: str
    text: str
    attachment_ids: list[str] = Field(default_factory=list)
    attachment_mode: Literal["auto", "reference_only", "inline"] = "auto"
    work_path: str | None = None
    delivery_mode: Literal["auto", "steer", "queue", "interrupt_and_send"] = "auto"
    routing_mode: Literal["auto", "pinned"] = "auto"


class SendMessageReceipt(StrictProtocolModel):
    """发送消息受理回执（202 Accepted）。"""

    client_message_id: str
    run_id: str
    resolved_conversation_id: str
    delivery_mode: Literal["started", "steered", "queued", "interrupted_and_started"]
    routing_action: Literal["CONTINUE", "SWITCH", "FORK_HANDOFF", "FORK_ROOT", "PINNED"]


class InterruptResponse(StrictProtocolModel):
    """中断前台 Router 生成响应。"""

    conversation_id: str
    interrupted: bool
    run_id: str | None = None
    head_entry_id: str | None = None


class RuntimeSnapshotView(StrictProtocolModel):
    """首屏全量水合快照（GET /v1/runtime/snapshot）。"""

    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    latest_seq: int
    trusted_local: bool
    model_name: str
    tree: TreeView
    tasks: list[TaskView] = Field(default_factory=list)
    pending_interactions: list[InteractionView] = Field(default_factory=list)
    workspace_locks: list[WorkspaceLockView] = Field(default_factory=list)
    context_snapshots: dict[str, ContextWindowSnapshotView] = Field(default_factory=dict)
    pool_status: LlmPoolStatusView
    recovery: list[RecoveredBranchView] = Field(default_factory=list)


class HealthView(StrictProtocolModel):
    """服务健康检查视图。"""

    status: Literal["ok"] = "ok"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    pid: int
    model_name: str
    trusted_local: bool
    active_conversation_id: str | None = None


class SubscriptionUpdateRequest(StrictProtocolModel):
    """动态更新 SSE 连接订阅的后台 Task 细粒度输出流列表。"""

    subscription_id: str
    task_ids: list[str] = Field(default_factory=list)
