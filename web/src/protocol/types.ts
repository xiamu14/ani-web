export const PROTOCOL_VERSION = "1.0" as const;

export type TaskStatusLiteral =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type TaskLivePhaseLiteral =
  | "waiting_dependency"
  | "acquiring_lock"
  | "thinking"
  | "calling_tool"
  | "waiting_approval"
  | "waiting_user"
  | "retrying_rate_limit"
  | "wrapping_up"
  | "settled";

export type AttachmentKindLiteral =
  | "image"
  | "audio"
  | "video"
  | "document"
  | "binary";

export type ContextBucketCategory =
  | "system_prompt"
  | "environment_info"
  | "skills"
  | "mcp"
  | "builtin_tools"
  | "memory"
  | "attachments"
  | "messages"
  | "overhead";

export type InteractionKindLiteral =
  | "approval"
  | "ask_user"
  | "mcp_elicitation";

export type BranchModeLiteral = "root" | "root_only" | "handoff" | "full_fork";

export type PartKindLiteral =
  | "text"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "file"
  | "system_notice";

export interface ErrorEnvelope {
  code: string;
  detail: Record<string, unknown>;
  message: string;
  protocol_version: "1.0";
  request_id: string;
  retryable: boolean;
}

export interface SuggestedAction {
  action_id: string;
  action_type: "steer" | "cancel" | "fork" | "switch" | "send_message";
  conversation_id: string;
  label: string;
  payload: Record<string, unknown>;
  task_id?: string | null;
}

export interface AttachmentView {
  attachment_id: string;
  byte_size: number;
  content_url: string;
  duration_sec?: number | null;
  height?: number | null;
  kind: AttachmentKindLiteral;
  media_type: string;
  page_count?: number | null;
  reference_handle: string;
  sha256: string;
  summary: string;
  title: string;
  width?: number | null;
}

export interface ArtifactView {
  artifact_id: string;
  byte_size: number;
  description: string;
  reference_handle: string;
  scope: string;
  sha256: string;
  value?: unknown;
}

export interface CitationLocatorView {
  artifact_key_path?: string | null;
  bbox?: number[] | null;
  line_end?: number | null;
  line_start?: number | null;
  page?: number | null;
  time_end_sec?: number | null;
  time_start_sec?: number | null;
}

export interface VerifiedCitationView {
  excerpt: string;
  locator: CitationLocatorView;
  raw_tag: string;
  target_id: string;
  target_kind: "attachment" | "artifact";
  verification_reason: string;
  verified: boolean;
}

export interface ContextSubItemView {
  detail: string;
  name: string;
  scaled: boolean;
  tokens: number;
}

export interface ContextBucketView {
  category: ContextBucketCategory;
  ratio: number;
  sub_items: ContextSubItemView[];
  tokens: number;
}

export interface ContextSavingsView {
  deferred_mcp_tokens_saved_est: number;
  deferred_mcp_tools_count: number;
  deferred_skills_count: number;
  deferred_skills_tokens_saved_est: number;
  opt6_omitted_media_count: number;
}

export interface ContextWindowSnapshotView {
  cache_hit_ratio: number;
  calibrated: boolean;
  categories: ContextBucketView[];
  conversation_id: string;
  estimation_delta: number;
  limit_known: boolean;
  model_name: string;
  run_id: string;
  savings: ContextSavingsView;
  task_id?: string | null;
  tokenizer_name: string;
  ts: string;
  uncached_input_tokens: number;
  utilization_ratio: number;
  window_limit_tokens?: number | null;
  window_used_tokens: number;
}

export interface BudgetProgressView {
  requests_ratio: number;
  soft_limit_reached: boolean;
  tokens_ratio: number;
  tool_calls_ratio: number;
}

export interface RunUsageView {
  budget_progress?: BudgetProgressView | null;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd?: number | null;
  input_tokens: number;
  output_reasoning_tokens: number;
  output_tokens: number;
  requests: number;
  tool_calls: number;
  total_tokens: number;
}

export interface PoolStatsView {
  current_in_flight: number;
  peak_in_flight: number;
  rate_limit_retries: number;
  total_requests: number;
}

export interface LlmPoolStatusView {
  cooldown_remaining_sec: number;
  jev_pool?: PoolStatsView | null;
  role_pool: PoolStatsView;
  router_pool: PoolStatsView;
  total_429_hits: number;
}

export interface RecoveredBranchView {
  interrupted_task_ids: string[];
  main_conversation_id: string;
  requeued_task_ids: string[];
}

export interface ActiveToolView {
  args_preview: string;
  started_at: string;
  tool_call_id: string;
  tool_name: string;
}

export interface ToolStepSummaryView {
  duration_ms: number;
  error_preview: string;
  succeeded: boolean;
  tool_name: string;
}

export interface TaskView {
  active_tool?: ActiveToolView | null;
  blocked_by: string[];
  created_at: string;
  current_step: number;
  depends_on: string[];
  live_phase: TaskLivePhaseLiteral;
  lock_mode: "exclusive" | "shared" | "none";
  main_conversation_id: string;
  mandate: string;
  max_requests: number;
  max_tool_calls: number;
  pending_interaction_ids: string[];
  queue_position: number;
  recent_steps: ToolStepSummaryView[];
  result_summary?: string | null;
  role_conversation_id?: string | null;
  role_name: string;
  soft_limit_triggered: boolean;
  status: TaskStatusLiteral;
  task_id: string;
  tool_calls_count: number;
  updated_at: string;
  usage: RunUsageView;
  work_path?: string | null;
}

export interface WorkspaceLockView {
  exclusive_holder?: string | null;
  queued_tasks: string[];
  shared_holders: string[];
  work_path: string;
}

export interface ApprovalItemView {
  args: Record<string, unknown>;
  category: string;
  item_id: string;
  reason: string;
  risk_level: "low" | "medium" | "high";
  rule_id: string;
  tool_name: string;
}

export interface ApprovalContextView {
  main_conversation_id: string;
  role_name?: string | null;
  run_id: string;
  task_id?: string | null;
  user_intent: string;
}

export interface ApprovalPayloadView {
  context: ApprovalContextView;
  items: ApprovalItemView[];
}

export interface AskUserOptionView {
  description?: string | null;
  label: string;
}

export interface AskUserQuestionView {
  header: string;
  multi_select: boolean;
  options: AskUserOptionView[];
  question: string;
}

export interface AskUserPayloadView {
  questions: AskUserQuestionView[];
  request_id: string;
}

export interface McpElicitationPayloadView {
  message: string;
  requested_schema: Record<string, unknown>;
  server_name: string;
}

export interface InteractionView {
  approval?: ApprovalPayloadView | null;
  ask_user?: AskUserPayloadView | null;
  conversation_id: string;
  created_at: string;
  interaction_id: string;
  kind: InteractionKindLiteral;
  mcp_elicitation?: McpElicitationPayloadView | null;
  role_name?: string | null;
  run_id?: string | null;
  status: "pending" | "resolved" | "cancelled";
  task_id?: string | null;
}

export interface ItemApprovalDecisionModel {
  approved: boolean;
  grant_category_for_run?: boolean;
  reason?: string;
}

export type InteractionRespondRequest =
  | {
      kind: "approval";
      item_decisions?: Record<string, ItemApprovalDecisionModel>;
      approved?: boolean | null;
      grant_categories_for_run?: string[];
      reason?: string;
    }
  | {
      kind: "ask_user";
      cancelled?: boolean;
      answers: Array<{
        header: string;
        selected?: string[];
        custom_answer?: string | null;
        note_text?: string;
        attachment_ids?: string[];
      }>;
    }
  | {
      kind: "mcp_elicitation";
      action: "accept" | "decline" | "cancel";
      content?: Record<string, unknown>;
    };

export interface BranchView {
  active_task_count: number;
  branch_mode: BranchModeLiteral;
  conversation_id: string;
  forked_at?: string | null;
  forked_from?: string | null;
  head_entry_id?: string | null;
  is_running: boolean;
  message_count: number;
  parent_conversation_id?: string | null;
  pending_interaction_count: number;
  role_name?: string | null;
  scope: string;
  status: string;
  target_title: string;
  task_id?: string | null;
  updated_at: string;
  usage_summary: RunUsageView;
  work_path?: string | null;
}

export interface TreeView {
  active_conversation_id?: string | null;
  branches: BranchView[];
  root_entry_id?: string | null;
}

export interface EntryPartView {
  attachment_id?: string | null;
  content_url?: string | null;
  part_kind: string;
  role: "system" | "user" | "assistant" | "tool";
  text?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
}

export interface EntryView {
  attached_task_ids: string[];
  child_branch_ids: string[];
  created_at: string;
  entry_id: string;
  kind: "root" | "conv_init" | "message" | "handoff";
  parent_id?: string | null;
  parts: EntryPartView[];
  run_id?: string | null;
  stop_backtrack: boolean;
}

export interface EntryPageView {
  conversation_id: string;
  entries: EntryView[];
  has_more: boolean;
  next_before_entry_id?: string | null;
}

export interface SendMessageRequest {
  attachment_ids?: string[];
  attachment_mode?: "auto" | "reference_only" | "inline";
  client_message_id: string;
  delivery_mode?: "auto" | "steer" | "queue" | "interrupt_and_send";
  routing_mode?: "auto" | "pinned";
  text: string;
  work_path?: string | null;
}

export interface SendMessageReceipt {
  client_message_id: string;
  delivery_mode: "started" | "steered" | "queued" | "interrupted_and_started";
  resolved_conversation_id: string;
  routing_action:
    | "CONTINUE"
    | "SWITCH"
    | "FORK_HANDOFF"
    | "FORK_ROOT"
    | "PINNED";
  run_id: string;
}

export interface InterruptResponse {
  conversation_id: string;
  head_entry_id?: string | null;
  interrupted: boolean;
  run_id?: string | null;
}

export interface RuntimeSnapshotView {
  context_snapshots: Record<string, ContextWindowSnapshotView>;
  latest_seq: number;
  model_name: string;
  pending_interactions: InteractionView[];
  pool_status: LlmPoolStatusView;
  protocol_version: "1.0";
  recovery: RecoveredBranchView[];
  tasks: TaskView[];
  tree: TreeView;
  trusted_local: boolean;
  workspace_locks: WorkspaceLockView[];
}

export interface HealthView {
  active_conversation_id?: string | null;
  model_name: string;
  pid: number;
  protocol_version: "1.0";
  status: "ok";
  trusted_local: boolean;
}

export interface EventEnvelopeBase {
  channel: "main" | "task_stream";
  conversation_id?: string | null;
  event_id: string;
  protocol_version: "1.0";
  role_name?: string | null;
  run_id?: string | null;
  seq: number;
  task_id?: string | null;
  ts: string;
}

export type ProtocolEvent = EventEnvelopeBase &
  (
    | {
        type: "runtime.ready";
        payload: {
          protocol_version: "1.0";
          latest_seq: number;
          active_conversation_id?: string | null;
          model_name: string;
          trusted_local: boolean;
          subscription_id: string;
        };
      }
    | {
        type: "runtime.recovery";
        payload: { recovered_branches: RecoveredBranchView[] };
      }
    | {
        type: "stream.resync_required";
        payload: {
          requested_after_seq: number;
          oldest_available_seq: number;
          latest_seq: number;
        };
      }
    | {
        type: "run.started";
        payload: {
          agent_kind: "router" | "role";
          proactive: boolean;
          entry_id?: string | null;
          user_message_preview: string;
        };
      }
    | {
        type: "message.part.start";
        payload: {
          part_index: number;
          part_kind: PartKindLiteral;
          tool_name?: string | null;
          tool_call_id?: string | null;
          attachment_id?: string | null;
          proactive: boolean;
        };
      }
    | {
        type: "message.part.delta";
        payload: {
          part_index: number;
          part_kind: PartKindLiteral;
          delta: string;
          tool_call_id?: string | null;
          proactive: boolean;
        };
      }
    | {
        type: "message.part.end";
        payload: {
          part_index: number;
          part_kind: PartKindLiteral;
          tool_name?: string | null;
          tool_call_id?: string | null;
          args_preview?: string | null;
          result_preview?: string | null;
          is_error: boolean;
          proactive: boolean;
        };
      }
    | {
        type: "run.completed";
        payload: {
          output_text: string;
          citations: VerifiedCitationView[];
          suggested_actions: SuggestedAction[];
          usage: RunUsageView;
          head_entry_id?: string | null;
          proactive: boolean;
        };
      }
    | {
        type: "run.interrupted";
        payload: {
          reason: "user_interrupt" | "shutdown" | "task_cancelled";
          repaired_message_count: number;
          head_entry_id?: string | null;
        };
      }
    | {
        type: "run.failed";
        payload: {
          error_code: string;
          error_message: string;
          abort_kind?: string | null;
          suggested_actions: SuggestedAction[];
        };
      }
    | {
        type: "interaction.requested";
        payload: { interaction: InteractionView };
      }
    | {
        type: "interaction.resolved";
        payload: {
          interaction_id: string;
          kind: InteractionKindLiteral;
          resolution_summary: string;
          resolved_by: string;
        };
      }
    | {
        type: "interaction.cancelled";
        payload: {
          interaction_id: string;
          kind: InteractionKindLiteral;
          reason: "run_cancelled" | "timed_out" | "shutdown";
        };
      }
    | { type: "task.created"; payload: { task: TaskView } }
    | {
        type: "task.status_changed";
        payload: {
          task: TaskView;
          previous_status?: TaskStatusLiteral | null;
          previous_phase?: TaskLivePhaseLiteral | null;
        };
      }
    | {
        type: "task.progress";
        payload: {
          task_id: string;
          live_phase: TaskLivePhaseLiteral;
          current_step: number;
          max_requests: number;
          tool_calls_count: number;
          max_tool_calls: number;
          soft_limit_triggered: boolean;
          active_tool?: ActiveToolView | null;
          recent_step?: ToolStepSummaryView | null;
        };
      }
    | {
        type: "task.steered";
        payload: {
          task_id: string;
          mode: "live_steer" | "queued_merge" | "revise_restart";
          instruction_preview: string;
        };
      }
    | {
        type: "workspace.locks_changed";
        payload: { locks: WorkspaceLockView[] };
      }
    | {
        type: "tree.branch_created";
        payload: {
          branch: BranchView;
          reason: "user_create" | "user_fork" | "semantic_fork";
        };
      }
    | {
        type: "tree.branch_switched";
        payload: {
          previous_conversation_id?: string | null;
          active_conversation_id: string;
          reason:
            | "user_switch"
            | "semantic_switch"
            | "user_fork"
            | "user_create";
        };
      }
    | { type: "tree.branch_updated"; payload: { branch: BranchView } }
    | {
        type: "tree.entry_appended";
        payload: {
          conversation_id: string;
          entries: EntryView[];
          head_entry_id?: string | null;
        };
      }
    | {
        type: "context.snapshot";
        payload: { snapshot: ContextWindowSnapshotView };
      }
    | {
        type: "usage.step_recorded";
        payload: {
          conversation_id: string;
          task_id?: string | null;
          run_id: string;
          step_usage: RunUsageView;
          run_usage: RunUsageView;
        };
      }
    | {
        type: "usage.budget_warning";
        payload: {
          conversation_id: string;
          task_id?: string | null;
          run_id: string;
          dimension: "requests" | "tool_calls" | "tokens";
          ratio: number;
        };
      }
    | { type: "attachment.registered"; payload: { attachment: AttachmentView } }
    | { type: "artifact.updated"; payload: { artifact: ArtifactView } }
    | { type: "llm.pool_status"; payload: { pool_status: LlmPoolStatusView } }
  );

export type SkillScopeLiteral = "workspace" | "global" | "builtin";

export interface SkillView {
  description: string;
  directory: string;
  model_invocable: boolean;
  name: string;
  path: string;
  scope: SkillScopeLiteral;
  version: string;
  when_to_use?: string | null;
}

export interface SkippedSkillView {
  message: string;
  path: string;
  reason: string;
}

export interface SkillsListView {
  skills: SkillView[];
  skipped: SkippedSkillView[];
  work_path?: string | null;
}

export interface McpToolPolicyView {
  level: string;
  lock_mode: string;
  prefixed_tool_name: string;
  reason: string;
  tool_name: string;
}

export interface McpServerView {
  blocked: boolean;
  command?: string | null;
  compile_error?: string | null;
  compile_status?: string | null;
  defer_loading: boolean;
  description?: string | null;
  name: string;
  roles?: string[] | null;
  scope: string;
  tools: McpToolPolicyView[];
  transport: string;
  url?: string | null;
}

export interface McpsListView {
  servers: McpServerView[];
  total_jev_calls: number;
}

export type TurnStepKindLiteral = "thinking" | "tool" | "text";
export type TurnToolStatusLiteral = "running" | "done" | "error";

export interface TurnStepView {
  args_text?: string | null;
  duration_ms?: number | null;
  kind: TurnStepKindLiteral;
  result_text?: string | null;
  status?: TurnToolStatusLiteral | null;
  text?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
}

export interface RunTurnView {
  attached_task_ids: string[];
  child_branch_ids: string[];
  created_at: string;
  run_id?: string | null;
  steps: TurnStepView[];
  user_entry_id?: string | null;
  user_text: string;
}

export interface TurnPageView {
  conversation_id: string;
  has_more: boolean;
  next_before_entry_id?: string | null;
  turns: RunTurnView[];
}
