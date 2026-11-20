"""Layer 0 SSE 事件信封与 8 大域判别联合事件模型（Rust serde 邻接标签兼容）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from agent.protocol.models import (
    PROTOCOL_VERSION,
    ActiveToolView,
    ArtifactView,
    AttachmentView,
    BranchView,
    ContextWindowSnapshotView,
    EntryView,
    InteractionKindLiteral,
    InteractionView,
    LlmPoolStatusView,
    RecoveredBranchView,
    RunUsageView,
    StrictProtocolModel,
    SuggestedAction,
    TaskLivePhaseLiteral,
    TaskStatusLiteral,
    TaskView,
    ToolStepSummaryView,
    TreeUsageSummaryView,
    VerifiedCitationView,
    WorkspaceLockView,
)

EventChannelLiteral = Literal["main", "task_stream"]


# ---------------------------------------------------------------------------
# 1. Connection & Recovery Payloads
# ---------------------------------------------------------------------------


class RuntimeReadyPayload(StrictProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    latest_seq: int
    active_conversation_id: str | None = None
    model_name: str
    trusted_local: bool
    subscription_id: str


class RuntimeRecoveryPayload(StrictProtocolModel):
    recovered_branches: list[RecoveredBranchView] = Field(default_factory=list)


class StreamResyncRequiredPayload(StrictProtocolModel):
    requested_after_seq: int
    oldest_available_seq: int
    latest_seq: int


# ---------------------------------------------------------------------------
# 2. Message & Streaming Run Payloads
# ---------------------------------------------------------------------------


PartKindLiteral = Literal[
    "text", "thinking", "tool_call", "tool_result", "file", "system_notice"
]


class RunStartedPayload(StrictProtocolModel):
    agent_kind: Literal["router", "role"]
    proactive: bool = False
    entry_id: str | None = None
    user_message_preview: str = ""


class MessagePartStartPayload(StrictProtocolModel):
    part_index: int
    part_kind: PartKindLiteral
    tool_name: str | None = None
    tool_call_id: str | None = None
    attachment_id: str | None = None
    proactive: bool = False


class MessagePartDeltaPayload(StrictProtocolModel):
    part_index: int
    part_kind: PartKindLiteral
    delta: str
    tool_call_id: str | None = None
    proactive: bool = False


class MessagePartEndPayload(StrictProtocolModel):
    part_index: int
    part_kind: PartKindLiteral
    tool_name: str | None = None
    tool_call_id: str | None = None
    args_preview: str | None = None
    result_preview: str | None = None
    is_error: bool = False
    proactive: bool = False


class RunCompletedPayload(StrictProtocolModel):
    output_text: str
    citations: list[VerifiedCitationView] = Field(default_factory=list)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    usage: RunUsageView = Field(default_factory=RunUsageView)
    head_entry_id: str | None = None
    proactive: bool = False


class RunInterruptedPayload(StrictProtocolModel):
    reason: Literal["user_interrupt", "shutdown", "task_cancelled"]
    repaired_message_count: int = 0
    head_entry_id: str | None = None


class RunFailedPayload(StrictProtocolModel):
    error_code: str
    error_message: str
    abort_kind: str | None = None
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 3. Interactions Payloads
# ---------------------------------------------------------------------------


class InteractionRequestedPayload(StrictProtocolModel):
    interaction: InteractionView


class InteractionResolvedPayload(StrictProtocolModel):
    interaction_id: str
    kind: InteractionKindLiteral
    resolution_summary: str = ""
    resolved_by: str = "user"


class InteractionCancelledPayload(StrictProtocolModel):
    interaction_id: str
    kind: InteractionKindLiteral
    reason: Literal["run_cancelled", "timed_out", "shutdown"]


# ---------------------------------------------------------------------------
# 4. Role Tasks & Workspace Locks Payloads
# ---------------------------------------------------------------------------


class TaskCreatedPayload(StrictProtocolModel):
    task: TaskView


class TaskStatusChangedPayload(StrictProtocolModel):
    task: TaskView
    previous_status: TaskStatusLiteral | None = None
    previous_phase: TaskLivePhaseLiteral | None = None


class TaskProgressPayload(StrictProtocolModel):
    task_id: str
    live_phase: TaskLivePhaseLiteral
    current_step: int = 0
    max_requests: int = 35
    tool_calls_count: int = 0
    max_tool_calls: int = 80
    soft_limit_triggered: bool = False
    active_tool: ActiveToolView | None = None
    recent_step: ToolStepSummaryView | None = None


class TaskSteeredPayload(StrictProtocolModel):
    task_id: str
    mode: Literal["live_steer", "queued_merge", "revise_restart"]
    instruction_preview: str


class WorkspaceLocksChangedPayload(StrictProtocolModel):
    locks: list[WorkspaceLockView] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 5. Conversation Tree Payloads
# ---------------------------------------------------------------------------


class TreeBranchCreatedPayload(StrictProtocolModel):
    branch: BranchView
    reason: Literal["user_create", "user_fork", "semantic_fork"]


class TreeBranchSwitchedPayload(StrictProtocolModel):
    previous_conversation_id: str | None = None
    active_conversation_id: str
    reason: Literal["user_switch", "semantic_switch", "user_fork", "user_create"]


class TreeBranchUpdatedPayload(StrictProtocolModel):
    branch: BranchView


class TreeEntryAppendedPayload(StrictProtocolModel):
    conversation_id: str
    entries: list[EntryView] = Field(default_factory=list)
    head_entry_id: str | None = None


# ---------------------------------------------------------------------------
# 6. Context & Usage Payloads
# ---------------------------------------------------------------------------


class ContextSnapshotPayload(StrictProtocolModel):
    snapshot: ContextWindowSnapshotView


class UsageStepRecordedPayload(StrictProtocolModel):
    conversation_id: str
    task_id: str | None = None
    run_id: str
    step_usage: RunUsageView
    run_usage: RunUsageView
    tree_usage: TreeUsageSummaryView | None = None


class UsageBudgetWarningPayload(StrictProtocolModel):
    conversation_id: str
    task_id: str | None = None
    run_id: str
    dimension: Literal["requests", "tool_calls", "tokens"]
    ratio: float


# ---------------------------------------------------------------------------
# 7. Attachments & Artifacts Payloads
# ---------------------------------------------------------------------------


class AttachmentRegisteredPayload(StrictProtocolModel):
    attachment: AttachmentView


class ArtifactUpdatedPayload(StrictProtocolModel):
    artifact: ArtifactView


# ---------------------------------------------------------------------------
# 8. LLM Pool Status Payload
# ---------------------------------------------------------------------------


class LlmPoolStatusPayload(StrictProtocolModel):
    pool_status: LlmPoolStatusView


# ---------------------------------------------------------------------------
# Discriminated ProtocolEvent Union (Rust serde / TypeScript Single Source)
# ---------------------------------------------------------------------------


class _EventEnvelopeBase(StrictProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    seq: int
    event_id: str
    ts: str
    conversation_id: str | None = None
    task_id: str | None = None
    role_name: str | None = None
    run_id: str | None = None
    channel: EventChannelLiteral = "main"


class RuntimeReadyEvent(_EventEnvelopeBase):
    type: Literal["runtime.ready"] = "runtime.ready"
    payload: RuntimeReadyPayload


class RuntimeRecoveryEvent(_EventEnvelopeBase):
    type: Literal["runtime.recovery"] = "runtime.recovery"
    payload: RuntimeRecoveryPayload


class StreamResyncRequiredEvent(_EventEnvelopeBase):
    type: Literal["stream.resync_required"] = "stream.resync_required"
    payload: StreamResyncRequiredPayload


class RunStartedEvent(_EventEnvelopeBase):
    type: Literal["run.started"] = "run.started"
    payload: RunStartedPayload


class MessagePartStartEvent(_EventEnvelopeBase):
    type: Literal["message.part.start"] = "message.part.start"
    payload: MessagePartStartPayload


class MessagePartDeltaEvent(_EventEnvelopeBase):
    type: Literal["message.part.delta"] = "message.part.delta"
    payload: MessagePartDeltaPayload


class MessagePartEndEvent(_EventEnvelopeBase):
    type: Literal["message.part.end"] = "message.part.end"
    payload: MessagePartEndPayload


class RunCompletedEvent(_EventEnvelopeBase):
    type: Literal["run.completed"] = "run.completed"
    payload: RunCompletedPayload


class RunInterruptedEvent(_EventEnvelopeBase):
    type: Literal["run.interrupted"] = "run.interrupted"
    payload: RunInterruptedPayload


class RunFailedEvent(_EventEnvelopeBase):
    type: Literal["run.failed"] = "run.failed"
    payload: RunFailedPayload


class InteractionRequestedEvent(_EventEnvelopeBase):
    type: Literal["interaction.requested"] = "interaction.requested"
    payload: InteractionRequestedPayload


class InteractionResolvedEvent(_EventEnvelopeBase):
    type: Literal["interaction.resolved"] = "interaction.resolved"
    payload: InteractionResolvedPayload


class InteractionCancelledEvent(_EventEnvelopeBase):
    type: Literal["interaction.cancelled"] = "interaction.cancelled"
    payload: InteractionCancelledPayload


class TaskCreatedEvent(_EventEnvelopeBase):
    type: Literal["task.created"] = "task.created"
    payload: TaskCreatedPayload


class TaskStatusChangedEvent(_EventEnvelopeBase):
    type: Literal["task.status_changed"] = "task.status_changed"
    payload: TaskStatusChangedPayload


class TaskProgressEvent(_EventEnvelopeBase):
    type: Literal["task.progress"] = "task.progress"
    payload: TaskProgressPayload


class TaskSteeredEvent(_EventEnvelopeBase):
    type: Literal["task.steered"] = "task.steered"
    payload: TaskSteeredPayload


class WorkspaceLocksChangedEvent(_EventEnvelopeBase):
    type: Literal["workspace.locks_changed"] = "workspace.locks_changed"
    payload: WorkspaceLocksChangedPayload


class TreeBranchCreatedEvent(_EventEnvelopeBase):
    type: Literal["tree.branch_created"] = "tree.branch_created"
    payload: TreeBranchCreatedPayload


class TreeBranchSwitchedEvent(_EventEnvelopeBase):
    type: Literal["tree.branch_switched"] = "tree.branch_switched"
    payload: TreeBranchSwitchedPayload


class TreeBranchUpdatedEvent(_EventEnvelopeBase):
    type: Literal["tree.branch_updated"] = "tree.branch_updated"
    payload: TreeBranchUpdatedPayload


class TreeEntryAppendedEvent(_EventEnvelopeBase):
    type: Literal["tree.entry_appended"] = "tree.entry_appended"
    payload: TreeEntryAppendedPayload


class ContextSnapshotEvent(_EventEnvelopeBase):
    type: Literal["context.snapshot"] = "context.snapshot"
    payload: ContextSnapshotPayload


class UsageStepRecordedEvent(_EventEnvelopeBase):
    type: Literal["usage.step_recorded"] = "usage.step_recorded"
    payload: UsageStepRecordedPayload


class UsageBudgetWarningEvent(_EventEnvelopeBase):
    type: Literal["usage.budget_warning"] = "usage.budget_warning"
    payload: UsageBudgetWarningPayload


class AttachmentRegisteredEvent(_EventEnvelopeBase):
    type: Literal["attachment.registered"] = "attachment.registered"
    payload: AttachmentRegisteredPayload


class ArtifactUpdatedEvent(_EventEnvelopeBase):
    type: Literal["artifact.updated"] = "artifact.updated"
    payload: ArtifactUpdatedPayload


class LlmPoolStatusEvent(_EventEnvelopeBase):
    type: Literal["llm.pool_status"] = "llm.pool_status"
    payload: LlmPoolStatusPayload


ProtocolEvent = Annotated[
    RuntimeReadyEvent
    | RuntimeRecoveryEvent
    | StreamResyncRequiredEvent
    | RunStartedEvent
    | MessagePartStartEvent
    | MessagePartDeltaEvent
    | MessagePartEndEvent
    | RunCompletedEvent
    | RunInterruptedEvent
    | RunFailedEvent
    | InteractionRequestedEvent
    | InteractionResolvedEvent
    | InteractionCancelledEvent
    | TaskCreatedEvent
    | TaskStatusChangedEvent
    | TaskProgressEvent
    | TaskSteeredEvent
    | WorkspaceLocksChangedEvent
    | TreeBranchCreatedEvent
    | TreeBranchSwitchedEvent
    | TreeBranchUpdatedEvent
    | TreeEntryAppendedEvent
    | ContextSnapshotEvent
    | UsageStepRecordedEvent
    | UsageBudgetWarningEvent
    | AttachmentRegisteredEvent
    | ArtifactUpdatedEvent
    | LlmPoolStatusEvent,
    Field(discriminator="type"),
]
