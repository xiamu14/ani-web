import type {
  ArtifactView,
  BranchView,
  ContextWindowSnapshotView,
  EntryPartView,
  EntryView,
  RunTurnView,
  TaskStatusLiteral,
  TaskView,
  TreeView,
  TurnStepView,
  WorkspaceLockView,
} from "@/protocol/types";
import type {
  ArtifactFormat,
  ArtifactItem,
  ChatMessage,
  ContextWindowSnapshot,
  ConversationTreeNode,
  LockMode,
  RoleName,
  RoleTask,
  RouteBranch,
  TaskStatus,
  ToolTrace,
  TurnStep,
} from "@/schemas/workspace";

const KNOWN_ROLES = new Set<RoleName>([
  "coder",
  "reviewer",
  "tester",
  "researcher",
]);
const TICKET_REPORT_HEADER_REGEX = /@(?<task>T\d+)\s*\(@(?<role>[a-z]+)\)/u;

function detectTicketReportMeta(text: string): {
  isTicketReport: boolean;
  role?: RoleName;
  taskCode?: string;
} {
  if (
    !(text.includes("<ticket_report") || text.startsWith("后台任务有新进展"))
  ) {
    return { isTicketReport: false };
  }
  const match = TICKET_REPORT_HEADER_REGEX.exec(text);
  const rawRole = match?.groups?.role;
  const rawTask = match?.groups?.task;
  return {
    isTicketReport: true,
    ...(rawRole && KNOWN_ROLES.has(rawRole as RoleName)
      ? { role: rawRole as RoleName }
      : {}),
    ...(rawTask ? { taskCode: `@${rawTask}` } : {}),
  };
}

function normalizeRole(role?: string | null): RoleName {
  if (role && KNOWN_ROLES.has(role as RoleName)) {
    return role as RoleName;
  }
  return "coder";
}

function normalizeTaskStatus(status: TaskStatusLiteral | string): TaskStatus {
  switch (status) {
    case "running":
    case "queued":
    case "completed":
    case "failed":
    case "interrupted":
    case "cancelled":
      return status;
    default:
      return "queued";
  }
}

function normalizeArtifactFormat(hint: string): ArtifactFormat {
  if (hint.includes("json")) {
    return "json";
  }
  if (hint.includes("diff") || hint.includes("patch")) {
    return "diff";
  }
  if (hint.includes("markdown") || hint.endsWith(".md")) {
    return "markdown";
  }
  return "code";
}

export function formatByteSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function parseTimestampMs(timestamp: number | string): number {
  if (typeof timestamp === "number") {
    return timestamp > 1e12 ? timestamp : timestamp * 1000;
  }
  return Date.parse(timestamp);
}

export function formatRelativeTime(timestamp?: number | string | null): string {
  if (timestamp === null || timestamp === undefined) {
    return "刚刚";
  }
  const ms = parseTimestampMs(timestamp);
  if (Number.isNaN(ms)) {
    return "刚刚";
  }
  const diffSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (diffSec < 60) {
    return "刚刚";
  }
  if (diffSec < 3600) {
    return `${Math.floor(diffSec / 60)}m 前`;
  }
  if (diffSec < 86_400) {
    return `${Math.floor(diffSec / 3600)}h 前`;
  }
  return `${Math.floor(diffSec / 86_400)}d 前`;
}

export function mapBranchToRoute(
  branch: BranchView,
  runningConversationIds: ReadonlySet<string>,
  activeConversationId: string | null
): RouteBranch {
  let status: RouteBranch["status"] = "idle";
  if (branch.is_running || runningConversationIds.has(branch.conversation_id)) {
    status = "active";
  } else if (branch.conversation_id === activeConversationId) {
    status = "active";
  } else if (branch.pending_interaction_count > 0) {
    status = "waiting";
  }

  return {
    id: branch.conversation_id,
    title: branch.target_title || branch.conversation_id.slice(0, 8),
    workPath: branch.work_path || ".",
    status,
    headEntryId: branch.head_entry_id ?? "root",
    revision: branch.branch_mode,
    updatedAt: formatRelativeTime(branch.updated_at),
    messageCount: branch.message_count,
  };
}

export function mapTaskViewToRoleTask(
  task: TaskView,
  locks: readonly WorkspaceLockView[] = []
): RoleTask {
  let lockMode: LockMode = task.lock_mode;
  for (const lock of locks) {
    if (lock.exclusive_holder === task.task_id) {
      lockMode = "exclusive";
      break;
    }
    if (lock.shared_holders.includes(task.task_id)) {
      lockMode = "shared";
      break;
    }
  }

  return {
    id: `${task.main_conversation_id}:${task.task_id}`,
    routeId: task.main_conversation_id,
    taskCode: `@${task.task_id}`,
    role: normalizeRole(task.role_name),
    title: task.mandate || task.role_name,
    status: normalizeTaskStatus(task.status),
    lockMode,
    dependsOn: task.depends_on.map((dep) => `@${dep}`),
    workPath: task.work_path || ".",
    livePhase: task.live_phase,
    ...(task.role_conversation_id
      ? { roleConversationId: task.role_conversation_id }
      : {}),
    currentStep: task.current_step,
    maxRequests: task.max_requests,
    toolCallsCount: task.tool_calls_count,
    maxToolCalls: task.max_tool_calls,
    ...(task.active_tool?.tool_name
      ? { activeToolName: task.active_tool.tool_name }
      : {}),
    ...(task.result_summary ? { summary: task.result_summary } : {}),
    updatedAt: formatRelativeTime(task.updated_at),
  };
}

export function mapArtifactView(
  artifact: ArtifactView,
  defaultRouteId: string
): ArtifactItem {
  const routeId = artifact.scope.startsWith("conv:")
    ? artifact.scope.slice("conv:".length)
    : defaultRouteId;
  const title = artifact.description || artifact.artifact_id;
  return {
    id: artifact.artifact_id,
    routeId,
    artifactCode: `@${artifact.artifact_id.slice(0, 8)}`,
    title,
    format: normalizeArtifactFormat(
      `${artifact.reference_handle} ${artifact.description}`
    ),
    sizeLabel: formatByteSize(artifact.byte_size),
    summary: artifact.description || artifact.reference_handle,
    producedBy: "运行产物",
    createdAt: "刚刚",
  };
}

interface ExtractedParts {
  role: EntryPartView["role"];
  textChunks: string[];
  thinkingChunks: string[];
  toolTraces: ToolTrace[];
}

function appendEntryPart(acc: ExtractedParts, part: EntryPartView): void {
  const { role: partRole, part_kind, text, tool_call_id, tool_name } = part;
  acc.role = partRole;
  if (part_kind === "thinking" && text) {
    acc.thinkingChunks.push(text);
    return;
  }
  if (part_kind === "tool_call" || part_kind === "tool-call") {
    acc.toolTraces.push({
      id: tool_call_id || `tc-${acc.toolTraces.length}`,
      name: tool_name || "tool",
      status: "running",
      argsSummary: text || "",
      durationMs: 0,
    });
    return;
  }
  if (part_kind === "tool_result" || part_kind === "tool-return") {
    const existing = acc.toolTraces.find((trace) => trace.id === tool_call_id);
    if (existing) {
      existing.status = "done";
      if (text) {
        existing.argsSummary = text;
      }
      return;
    }
    acc.toolTraces.push({
      id: tool_call_id || `tr-${acc.toolTraces.length}`,
      name: tool_name || "tool",
      status: "done",
      argsSummary: text || "",
      durationMs: 0,
    });
    return;
  }
  if (text) {
    acc.textChunks.push(text);
  }
}

function extractPartsFromEntry(parts: readonly EntryPartView[]): {
  role: EntryPartView["role"];
  content: string;
  thinking: string;
  toolTraces: ToolTrace[];
} {
  const acc: ExtractedParts = {
    role: "assistant",
    textChunks: [],
    thinkingChunks: [],
    toolTraces: [],
  };

  for (const part of parts) {
    appendEntryPart(acc, part);
  }

  return {
    role: acc.role,
    content: acc.textChunks.join("\n"),
    thinking: acc.thinkingChunks.join("\n"),
    toolTraces: acc.toolTraces,
  };
}

export function mapEntryToChatMessage(
  entry: EntryView,
  threadId: string,
  roleHint?: RoleName
): ChatMessage {
  const extracted = extractPartsFromEntry(entry.parts);
  let sender: ChatMessage["sender"] = "router";
  let senderLabel = "主路由";
  let resolvedRole = roleHint;

  if (extracted.role === "user") {
    const ticketMeta = detectTicketReportMeta(extracted.content);
    if (ticketMeta.isTicketReport) {
      sender = ticketMeta.role ? "role" : "router";
      senderLabel = ticketMeta.taskCode
        ? `后台进展 ${ticketMeta.taskCode}`
        : "后台进展";
      resolvedRole = ticketMeta.role ?? resolvedRole;
    } else {
      sender = "user";
      senderLabel = "你";
    }
  } else if (extracted.role === "system" || entry.kind === "handoff") {
    sender = "system";
    senderLabel = "系统";
  } else if (resolvedRole) {
    sender = "role";
    senderLabel = `@${resolvedRole}`;
  }

  return {
    id: entry.entry_id,
    threadId,
    sender,
    senderLabel,
    ...(resolvedRole ? { role: resolvedRole } : {}),
    content: extracted.content,
    createdAt: formatRelativeTime(entry.created_at),
    ...(extracted.thinking ? { thinking: extracted.thinking } : {}),
    ...(entry.run_id ? { runId: entry.run_id } : {}),
    ...(extracted.toolTraces.length > 0
      ? { toolTraces: extracted.toolTraces }
      : {}),
  };
}

function mapTurnStep(step: TurnStepView, index: number): TurnStep {
  return {
    id: step.tool_call_id ?? `step-${index}`,
    kind: step.kind,
    ...(step.text ? { text: step.text } : {}),
    ...(step.tool_call_id ? { toolCallId: step.tool_call_id } : {}),
    ...(step.tool_name ? { toolName: step.tool_name } : {}),
    ...(step.status ? { status: step.status } : {}),
    ...(step.args_text ? { argsText: step.args_text } : {}),
    ...(step.result_text ? { resultText: step.result_text } : {}),
    ...(typeof step.duration_ms === "number"
      ? { durationMs: step.duration_ms }
      : {}),
  };
}

function buildTurnUserMessage(
  turn: RunTurnView,
  threadId: string,
  index: number,
  createdAt: string
): ChatMessage {
  const ticketMeta = detectTicketReportMeta(turn.user_text);
  if (ticketMeta.isTicketReport) {
    return {
      id: turn.user_entry_id ?? `turn-report-${index}`,
      threadId,
      sender: ticketMeta.role ? "role" : "router",
      senderLabel: ticketMeta.taskCode
        ? `后台进展 ${ticketMeta.taskCode}`
        : "后台进展",
      ...(ticketMeta.role ? { role: ticketMeta.role } : {}),
      content: turn.user_text,
      createdAt,
      proactive: true,
      ...(turn.run_id ? { runId: turn.run_id } : {}),
    };
  }
  return {
    id: turn.user_entry_id ?? `turn-user-${index}`,
    threadId,
    sender: "user",
    senderLabel: "你",
    content: turn.user_text,
    createdAt,
    ...(turn.run_id ? { runId: turn.run_id } : {}),
  };
}

export function mapTurnToChatMessages(
  turn: RunTurnView,
  threadId: string,
  index: number,
  roleHint?: RoleName
): ChatMessage[] {
  const result: ChatMessage[] = [];
  const createdAt = formatRelativeTime(turn.created_at);

  if (turn.user_text) {
    result.push(buildTurnUserMessage(turn, threadId, index, createdAt));
  }

  if (turn.steps.length > 0) {
    const mappedSteps = turn.steps.map(mapTurnStep);
    const content = mappedSteps
      .filter((step) => step.kind === "text" && step.text)
      .map((step) => step.text as string)
      .join("\n\n");
    const steeringSteps = mappedSteps.filter(
      (step) => step.kind === "thinking" || step.kind === "tool"
    );
    const sender: ChatMessage["sender"] = roleHint ? "role" : "router";
    const senderLabel = roleHint ? `@${roleHint}` : "主路由";

    result.push({
      id: turn.run_id ? `run-${turn.run_id}` : `turn-asst-${index}`,
      threadId,
      sender,
      senderLabel,
      ...(roleHint ? { role: roleHint } : {}),
      content,
      createdAt,
      ...(turn.run_id ? { runId: turn.run_id } : {}),
      ...(steeringSteps.length > 0 ? { steps: steeringSteps } : {}),
    });
  }

  return result;
}

export function mapContextSnapshot(
  snapshot: ContextWindowSnapshotView,
  label: string
): ContextWindowSnapshot {
  const bucketTokens = (cats: readonly string[]): number =>
    snapshot.categories
      .filter((bucket) => cats.includes(bucket.category))
      .reduce((sum, bucket) => sum + bucket.tokens, 0);

  return {
    threadId: snapshot.conversation_id,
    label,
    maxTokens: snapshot.window_limit_tokens || 200_000,
    usedTokens: snapshot.window_used_tokens,
    requests: 0,
    inputTokens: snapshot.uncached_input_tokens,
    outputTokens: 0,
    toolCalls: 0,
    segments: [
      {
        key: "system",
        label: "系统提示",
        tokens: bucketTokens(["system_prompt", "environment_info", "skills"]),
      },
      {
        key: "history",
        label: "对话附件",
        tokens: bucketTokens(["messages", "attachments", "overhead"]),
      },
      {
        key: "tools",
        label: "工具协议",
        tokens: bucketTokens(["builtin_tools", "mcp"]),
      },
      {
        key: "memory",
        label: "长期记忆",
        tokens: bucketTokens(["memory"]),
      },
    ],
  };
}

export function mapTreeToNodes(
  tree: TreeView,
  tasks: readonly RoleTask[],
  runningIds: ReadonlySet<string>,
  activeConversationId: string | null
): ConversationTreeNode[] {
  const branches = tree.branches.filter(
    (branch) => branch.scope === "main" && !branch.role_name && !branch.task_id
  );
  if (branches.length === 0) {
    return [];
  }

  const branchById = new Map<string, BranchView>(
    branches.map((branch) => [branch.conversation_id, branch])
  );
  const childrenByParent = new Map<string, BranchView[]>();
  const roots: BranchView[] = [];

  for (const branch of branches) {
    if (
      branch.parent_conversation_id &&
      branchById.has(branch.parent_conversation_id)
    ) {
      const list = childrenByParent.get(branch.parent_conversation_id) ?? [];
      list.push(branch);
      childrenByParent.set(branch.parent_conversation_id, list);
    } else {
      roots.push(branch);
    }
  }

  const buildBranchNode = (
    branch: BranchView,
    isRoot: boolean
  ): ConversationTreeNode => {
    const childBranches = (
      childrenByParent.get(branch.conversation_id) ?? []
    ).map((child) => buildBranchNode(child, false));
    const branchTasks: ConversationTreeNode[] = tasks
      .filter((task) => task.routeId === branch.conversation_id)
      .map((task) => {
        let status: ConversationTreeNode["status"] = "idle";
        if (task.status === "running") {
          status = "running";
        } else if (task.status === "queued") {
          status = "queued";
        } else if (task.status === "completed") {
          status = "completed";
        }
        return {
          id: `node-task-${task.id}`,
          label: `${task.taskCode} ${task.role}`,
          kind: "task",
          refId: task.id,
          entryId: task.roleConversationId ?? task.id,
          status,
          tokens: 0,
          isHead: task.status === "running",
        };
      });

    const isHead =
      branch.conversation_id === activeConversationId ||
      branch.conversation_id === tree.active_conversation_id;

    return {
      id: `node-conv-${branch.conversation_id}`,
      label: branch.target_title || branch.conversation_id.slice(0, 8),
      kind: isRoot ? "root" : "route",
      refId: branch.conversation_id,
      entryId: branch.head_entry_id ?? "root",
      status:
        branch.is_running || runningIds.has(branch.conversation_id) || isHead
          ? "active"
          : "idle",
      tokens: branch.usage_summary.total_tokens,
      isHead,
      children: [...branchTasks, ...childBranches],
    };
  };

  return roots.map((root, index) => buildBranchNode(root, index === 0));
}
