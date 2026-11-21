"""Layer 0 基础协议模型、附件/溯源、Context Window 9 桶与三层 Token 用量视图。"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

PROTOCOL_VERSION: Literal["1.0"] = "1.0"


class StrictProtocolModel(BaseModel):
    """所有协议模型的严格基类，禁止未声明字段。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class ErrorEnvelope(StrictProtocolModel):
    """统一 REST 错误响应信封。"""

    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    code: str
    message: str
    detail: dict[str, JsonValue] = Field(default_factory=dict)
    retryable: bool = False
    request_id: str


class SuggestedAction(StrictProtocolModel):
    """非阻塞快捷操作建议（区别于阻塞式 AskUser）。"""

    action_id: str
    label: str
    action_type: Literal["steer", "cancel", "fork", "switch", "send_message"]
    conversation_id: str
    task_id: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Attachments / Citations / Artifacts
# ---------------------------------------------------------------------------


AttachmentKindLiteral = Literal["image", "audio", "video", "document", "binary"]


class AttachmentView(StrictProtocolModel):
    """附件元数据视图。"""

    attachment_id: str
    sha256: str
    title: str
    media_type: str
    kind: AttachmentKindLiteral
    byte_size: int
    page_count: int | None = None
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    summary: str = ""
    reference_handle: str
    content_url: str


class LocalAttachmentRequest(StrictProtocolModel):
    """仅限 UDS 受信任连接使用的本地路径直注请求。"""

    file_path: str
    title: str | None = None
    media_type: str | None = None
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    summary: str = ""


class ArtifactView(StrictProtocolModel):
    """跨 Role 内部产物/状态变量视图。"""

    artifact_id: str
    scope: str
    byte_size: int
    sha256: str
    description: str = ""
    reference_handle: str
    value: JsonValue | None = None


class CitationLocatorView(StrictProtocolModel):
    """溯源物理定位器。"""

    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    time_start_sec: float | None = None
    time_end_sec: float | None = None
    bbox: list[float] | None = None
    artifact_key_path: str | None = None


class VerifiedCitationView(StrictProtocolModel):
    """经物理边界核验的生成结果溯源引用。"""

    raw_tag: str
    target_kind: Literal["attachment", "artifact"]
    target_id: str
    locator: CitationLocatorView
    excerpt: str
    verified: bool
    verification_reason: str = "ok"


# ---------------------------------------------------------------------------
# Context Window (9 Buckets) & 3-Tier Token Usage
# ---------------------------------------------------------------------------


ContextBucketCategory = Literal[
    "system_prompt",
    "environment_info",
    "skills",
    "mcp",
    "builtin_tools",
    "memory",
    "attachments",
    "messages",
    "overhead",
]


class ContextSubItemView(StrictProtocolModel):
    """单个分类桶内部的细分子项消耗。"""

    name: str
    tokens: int
    detail: str = ""
    scaled: bool = False


class ContextBucketView(StrictProtocolModel):
    """Context Window 9 大分类桶之一。"""

    category: ContextBucketCategory
    tokens: int
    ratio: float
    sub_items: list[ContextSubItemView] = Field(default_factory=list)


class ContextSavingsView(StrictProtocolModel):
    """延迟加载与 OPT-6 窗口优化省流指标。"""

    deferred_mcp_tools_count: int = 0
    deferred_mcp_tokens_saved_est: int = 0
    deferred_skills_count: int = 0
    deferred_skills_tokens_saved_est: int = 0
    opt6_omitted_media_count: int = 0


class ContextWindowSnapshotView(StrictProtocolModel):
    """单次模型请求的上下文窗口占用快照（Layer A）。"""

    conversation_id: str
    task_id: str | None = None
    run_id: str
    model_name: str
    window_used_tokens: int
    window_limit_tokens: int | None = None
    limit_known: bool = False
    utilization_ratio: float = 0.0
    cache_hit_ratio: float = 0.0
    uncached_input_tokens: int = 0
    calibrated: bool = False
    tokenizer_name: str = "o200k_base"
    estimation_delta: int = 0
    categories: list[ContextBucketView] = Field(default_factory=list)
    savings: ContextSavingsView = Field(default_factory=ContextSavingsView)
    ts: str


class BudgetProgressView(StrictProtocolModel):
    """对照 LoopBudgetPolicy 的预算消耗水位。"""

    requests_ratio: float = 0.0
    tool_calls_ratio: float = 0.0
    tokens_ratio: float = 0.0
    soft_limit_reached: bool = False


class RunUsageView(StrictProtocolModel):
    """单次 Run / Task 的累计用量（Layer B，严格遵循 UsageBase.input_tokens 包含式口径）。"""

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    output_reasoning_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    requests: int = 0
    tool_calls: int = 0
    budget_progress: BudgetProgressView | None = None


class AuxJevUsageView(StrictProtocolModel):
    """Jev 辅助决策模型用量（独立列项，不混入主模型窗口与账单）。"""

    source: Literal["semantic_router", "permission_review", "mcp_compile", "other"]
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None


class RoleTaskUsageItemView(StrictProtocolModel):
    """单个 Role Task 的用量明细。"""

    task_id: str
    role_name: str
    usage: RunUsageView


class TreeUsageSummaryView(StrictProtocolModel):
    """Conversation Tree 全局三层汇总（Layer C）。"""

    conversation_id: str
    main_router_usage: RunUsageView
    role_tasks_usage: list[RoleTaskUsageItemView] = Field(default_factory=list)
    role_tasks_total: RunUsageView = Field(default_factory=RunUsageView)
    auxiliary_jev_usage: list[AuxJevUsageView] = Field(default_factory=list)


class PoolStatsView(StrictProtocolModel):
    """单个 LLM 限流池水位。"""

    current_in_flight: int = 0
    peak_in_flight: int = 0
    total_requests: int = 0
    rate_limit_retries: int = 0


class LlmPoolStatusView(StrictProtocolModel):
    """全局 LLM 并发池与 429 冷却状态。"""

    router_pool: PoolStatsView
    role_pool: PoolStatsView
    jev_pool: PoolStatsView | None = None
    cooldown_remaining_sec: float = 0.0
    total_429_hits: int = 0


class RecoveredBranchView(StrictProtocolModel):
    """启动恢复的任务摘要。"""

    main_conversation_id: str
    interrupted_task_ids: list[str] = Field(default_factory=list)
    requeued_task_ids: list[str] = Field(default_factory=list)


SkillScopeLiteral = Literal["workspace", "global", "builtin"]


class SkillView(StrictProtocolModel):
    """已发现并仲裁生效的单个 Skill 元数据视图。"""

    name: str
    description: str
    when_to_use: str | None = None
    scope: SkillScopeLiteral
    version: str
    directory: str
    path: str
    model_invocable: bool = True


class SkippedSkillView(StrictProtocolModel):
    """因同名仲裁或校验被跳过的 Skill 记录。"""

    path: str
    reason: str
    message: str


class SkillsListView(StrictProtocolModel):
    """当前工作区/全局可见 Skills 清单。"""

    work_path: str | None = None
    skills: list[SkillView] = Field(default_factory=list)
    skipped: list[SkippedSkillView] = Field(default_factory=list)


class McpToolPolicyView(StrictProtocolModel):
    """单个 MCP 工具的静态权限策略视图。"""

    tool_name: str
    prefixed_tool_name: str
    level: str
    lock_mode: str
    reason: str = ""


class McpServerView(StrictProtocolModel):
    """已配置 MCP Server 的脱敏状态与工具策略视图。"""

    name: str
    scope: str
    transport: str
    description: str | None = None
    command: str | None = None
    url: str | None = None
    roles: list[str] | None = None
    defer_loading: bool = False
    blocked: bool = False
    compile_status: str | None = None
    compile_error: str | None = None
    tools: list[McpToolPolicyView] = Field(default_factory=list)


class McpsListView(StrictProtocolModel):
    """已配置的 MCP Servers 汇总视图。"""

    servers: list[McpServerView] = Field(default_factory=list)
    total_jev_calls: int = 0


TurnStepKindLiteral = Literal["thinking", "tool", "text"]
TurnToolStatusLiteral = Literal["running", "done", "error"]


class TurnStepView(StrictProtocolModel):
    """单轮对话内按发生顺序排列的统一步骤视图（历史与 SSE 实时共用）。"""

    kind: TurnStepKindLiteral
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    status: TurnToolStatusLiteral | None = None
    args_text: str | None = None
    result_text: str | None = None
    duration_ms: int | None = None


class RunTurnView(StrictProtocolModel):
    """按 run_id 聚合的单轮对话气泡视图。"""

    run_id: str | None = None
    user_entry_id: str | None = None
    user_text: str = ""
    steps: list[TurnStepView] = Field(default_factory=list)
    child_branch_ids: list[str] = Field(default_factory=list)
    attached_task_ids: list[str] = Field(default_factory=list)
    created_at: str = ""


class TurnPageView(StrictProtocolModel):
    """`/v1/conversations/{cid}/entries?view=turns` 分页响应。"""

    conversation_id: str
    turns: list[RunTurnView] = Field(default_factory=list)
    has_more: bool = False
    next_before_entry_id: str | None = None
