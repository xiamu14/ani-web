import { Store } from "@tanstack/react-store";
import { GuiProtocolClient } from "@/protocol/client";
import {
  mapArtifactView,
  mapBranchToRoute,
  mapContextSnapshot,
  mapEntryToChatMessage,
  mapTaskViewToRoleTask,
  mapTreeToNodes,
  mapTurnToChatMessages,
} from "@/protocol/mappers";
import {
  getProtocolClient,
  setProtocolClient,
  setSubscriptionId,
} from "@/protocol/runtime-client";
import { openProtocolSse } from "@/protocol/sse";
import type {
  BranchView,
  ContextWindowSnapshotView,
  LlmPoolStatusView,
  McpServerView,
  ProtocolEvent,
  RuntimeSnapshotView,
  SkillView,
  SkippedSkillView,
  TaskView,
  TreeView,
  WorkspaceLockView,
} from "@/protocol/types";
import type { ChatMessage, RoleName, RoleTask } from "@/schemas/workspace";
import {
  clearInteractions,
  removeInteraction,
  setPendingInteractions,
  upsertInteraction,
} from "@/store/interactions";
import { loadWorkspaceFiles, workspaceStore } from "@/store/workspace";

const STORAGE_KEY = "ani-web-connection";

export type ConnectionStatus =
  | "disconnected"
  | "connecting"
  | "connected"
  | "error";

export interface DiscoveredRuntime {
  httpUrl: string;
  pid: number | null;
  udsPath: string | null;
}

export interface ConnectionState {
  baseUrl: string;
  discoveredRuntime: DiscoveredRuntime | null;
  error: string | null;
  latestSeq: number;
  mcpServers: McpServerView[];
  modelName: string | null;
  poolStatus: LlmPoolStatusView | null;
  rawContextByThread: Record<string, ContextWindowSnapshotView>;
  runningConversationIds: string[];
  skills: SkillView[];
  skippedSkills: SkippedSkillView[];
  status: ConnectionStatus;
  subscriptionId: string | null;
  token: string;
  trustedLocal: boolean;
  workspaceLocks: WorkspaceLockView[];
}

function loadSavedConfig(): { baseUrl: string; token: string } {
  const defaultBaseUrl = import.meta.env.VITE_AGENT_URL ?? "";
  const defaultToken = import.meta.env.VITE_AGENT_TOKEN ?? "";
  if (typeof window === "undefined") {
    return { baseUrl: defaultBaseUrl, token: defaultToken };
  }
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { baseUrl: defaultBaseUrl, token: defaultToken };
    }
    const parsed = JSON.parse(raw) as { baseUrl?: string; token?: string };
    return {
      baseUrl: parsed.baseUrl ?? defaultBaseUrl,
      token: parsed.token ?? defaultToken,
    };
  } catch {
    return { baseUrl: defaultBaseUrl, token: defaultToken };
  }
}

function saveConfig(baseUrl: string, token: string): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ baseUrl, token })
    );
  } catch {
    // Ignore storage quota errors.
  }
}

const initialConfig = loadSavedConfig();

export const connectionStore = new Store<ConnectionState>({
  baseUrl: initialConfig.baseUrl,
  token: initialConfig.token,
  status: "disconnected",
  error: null,
  discoveredRuntime: null,
  modelName: null,
  trustedLocal: false,
  latestSeq: 0,
  mcpServers: [],
  subscriptionId: null,
  poolStatus: null,
  runningConversationIds: [],
  skills: [],
  skippedSkills: [],
  workspaceLocks: [],
  rawContextByThread: {},
});

let closeSseStream: (() => void) | null = null;
let connectGeneration = 0;
let latestTreeView: TreeView | null = null;

function syncTreeNodes(): void {
  if (!latestTreeView) {
    return;
  }
  const runningSet = new Set(connectionStore.state.runningConversationIds);
  const { activeRouteId, tasks } = workspaceStore.state;
  const treeNodes = mapTreeToNodes(
    latestTreeView,
    tasks,
    runningSet,
    activeRouteId || null
  );
  workspaceStore.setState((state) => ({
    ...state,
    treeNodes,
  }));
}

function resolveThreadIdFromEvent(event: ProtocolEvent): string | null {
  if (event.channel === "task_stream" && event.task_id) {
    const matchingTask = workspaceStore.state.tasks.find(
      (task) => task.taskCode === `@${event.task_id}`
    );
    if (matchingTask) {
      return matchingTask.id;
    }
  }
  return event.conversation_id ?? workspaceStore.state.activeRouteId;
}

function upsertStreamingAssistantMessage(
  threadId: string,
  runId: string | null | undefined,
  updater: (existing: ChatMessage) => ChatMessage
): void {
  workspaceStore.setState((state) => {
    const existingList = state.messagesByThread[threadId] ?? [];
    const targetId = runId ? `run-${runId}` : `stream-${threadId}`;
    const index = existingList.findIndex(
      (msg) =>
        msg.id === targetId || (runId && msg.runId === runId && msg.streaming)
    );

    let nextList: ChatMessage[];
    const existingMessage = index >= 0 ? existingList[index] : undefined;
    if (existingMessage) {
      const updated = updater(existingMessage);
      nextList = existingList.map((msg, i) => (i === index ? updated : msg));
    } else {
      const task = state.tasks.find((item) => item.id === threadId);
      const roleHint: RoleName | undefined = task?.role;
      const base: ChatMessage = {
        id: targetId,
        threadId,
        sender: roleHint ? "role" : "router",
        senderLabel: roleHint ? `@${roleHint}` : "主路由",
        ...(roleHint ? { role: roleHint } : {}),
        content: "",
        createdAt: "刚刚",
        ...(runId ? { runId } : {}),
        streaming: true,
      };
      nextList = [...existingList, updater(base)];
    }

    return {
      ...state,
      messagesByThread: {
        ...state.messagesByThread,
        [threadId]: nextList,
      },
    };
  });
}

function markConversationRunning(
  conversationId: string | null | undefined,
  running: boolean
): void {
  if (!conversationId) {
    return;
  }
  connectionStore.setState((state) => {
    if (running) {
      return state.runningConversationIds.includes(conversationId)
        ? state
        : {
            ...state,
            runningConversationIds: [
              ...state.runningConversationIds,
              conversationId,
            ],
          };
    }
    return {
      ...state,
      runningConversationIds: state.runningConversationIds.filter(
        (id) => id !== conversationId
      ),
    };
  });
}

function handlePartStart(
  threadId: string,
  event: Extract<ProtocolEvent, { type: "message.part.start" }>
): void {
  if (event.payload.part_kind === "thinking") {
    const stepId = `think-${event.payload.part_index}`;
    upsertStreamingAssistantMessage(threadId, event.run_id, (msg) => {
      const steps = msg.steps ?? [];
      if (steps.some((s) => s.id === stepId)) {
        return msg;
      }
      return {
        ...msg,
        steps: [...steps, { id: stepId, kind: "thinking", text: "" }],
      };
    });
    return;
  }
  if (event.payload.part_kind === "tool_call") {
    const traceId =
      event.payload.tool_call_id ?? `tc-${event.payload.part_index}`;
    const toolName = event.payload.tool_name ?? "tool";
    upsertStreamingAssistantMessage(threadId, event.run_id, (msg) => {
      const steps = msg.steps ?? [];
      const hasStep = steps.some((s) => s.id === traceId);
      return {
        ...msg,
        steps: hasStep
          ? steps
          : [
              ...steps,
              {
                id: traceId,
                kind: "tool",
                toolCallId: traceId,
                toolName,
                status: "running",
                argsText: "",
                durationMs: 0,
              },
            ],
      };
    });
  }
}

function handlePartDelta(
  threadId: string,
  event: Extract<ProtocolEvent, { type: "message.part.delta" }>
): void {
  const { part_index, part_kind, delta, tool_call_id } = event.payload;
  upsertStreamingAssistantMessage(threadId, event.run_id, (msg) => {
    if (part_kind === "thinking") {
      const stepId = `think-${part_index}`;
      const steps = (msg.steps ?? []).map((step) =>
        step.id === stepId
          ? { ...step, text: `${step.text ?? ""}${delta}` }
          : step
      );
      return { ...msg, steps };
    }
    if (part_kind === "tool_call" && tool_call_id) {
      const steps = (msg.steps ?? []).map((step) =>
        step.id === tool_call_id
          ? { ...step, argsText: `${step.argsText ?? ""}${delta}` }
          : step
      );
      return { ...msg, steps };
    }
    return { ...msg, content: `${msg.content}${delta}` };
  });
}

function handlePartEnd(
  threadId: string,
  event: Extract<ProtocolEvent, { type: "message.part.end" }>
): void {
  const isToolPart =
    event.payload.part_kind === "tool_call" ||
    event.payload.part_kind === "tool_result";
  if (!isToolPart) {
    return;
  }
  const callId = event.payload.tool_call_id;
  const isResult = event.payload.part_kind === "tool_result";
  upsertStreamingAssistantMessage(threadId, event.run_id, (msg) => ({
    ...msg,
    steps: (msg.steps ?? []).map((step) => {
      if (step.id !== callId) {
        return step;
      }
      let nextStatus = step.status;
      if (event.payload.is_error) {
        nextStatus = "error";
      } else if (isResult) {
        nextStatus = "done";
      }
      return {
        ...step,
        status: nextStatus,
        ...(event.payload.args_preview
          ? { argsText: event.payload.args_preview }
          : {}),
        ...(event.payload.result_preview
          ? { resultText: event.payload.result_preview }
          : {}),
      };
    }),
  }));
  if (isResult && event.payload.tool_name === "set_workspace") {
    void refreshRuntimeSnapshot();
  }
}

function handlePartEvent(event: ProtocolEvent): boolean {
  const threadId = resolveThreadIdFromEvent(event);
  if (event.type === "message.part.start") {
    if (threadId) {
      handlePartStart(threadId, event);
    }
    return true;
  }
  if (event.type === "message.part.delta") {
    if (threadId) {
      handlePartDelta(threadId, event);
    }
    return true;
  }
  if (event.type === "message.part.end") {
    if (threadId) {
      handlePartEnd(threadId, event);
    }
    return true;
  }
  return false;
}

function handleRunLifecycleEvent(event: ProtocolEvent): boolean {
  const threadId = resolveThreadIdFromEvent(event);
  if (event.type === "run.started") {
    markConversationRunning(event.conversation_id, true);
    if (threadId) {
      upsertStreamingAssistantMessage(threadId, event.run_id, (msg) => ({
        ...msg,
        streaming: true,
        ...(event.payload.proactive ? { proactive: true } : {}),
      }));
    }
    return true;
  }
  if (event.type === "run.completed") {
    markConversationRunning(event.conversation_id, false);
    if (threadId) {
      const { output_text, proactive, citations, suggested_actions } =
        event.payload;
      upsertStreamingAssistantMessage(threadId, event.run_id, (msg) => ({
        ...msg,
        content: output_text || msg.content,
        streaming: false,
        proactive,
        ...(citations.length > 0
          ? {
              citations: citations.map((cit, idx) => ({
                id: `${msg.id}-cit-${idx}`,
                sourceType: cit.target_kind,
                label: cit.target_id,
                summary: cit.excerpt,
              })),
            }
          : {}),
        ...(suggested_actions.length > 0
          ? {
              suggestedActions: suggested_actions.map((act) => ({
                id: act.action_id,
                label: act.label,
                action: act.action_type,
                payload: act.payload,
              })),
            }
          : {}),
      }));
    }
    return true;
  }
  if (event.type === "run.interrupted" || event.type === "run.failed") {
    markConversationRunning(event.conversation_id, false);
    if (threadId) {
      const errorText =
        event.type === "run.failed"
          ? event.payload.error_message
          : "运行已中断";
      upsertStreamingAssistantMessage(threadId, event.run_id, (msg) => ({
        ...msg,
        streaming: false,
        error: errorText,
      }));
    }
    return true;
  }
  return false;
}

function upsertRoleTask(taskView: TaskView): void {
  const locks = connectionStore.state.workspaceLocks;
  const mapped: RoleTask = mapTaskViewToRoleTask(taskView, locks);
  workspaceStore.setState((state) => {
    const exists = state.tasks.some((item) => item.id === mapped.id);
    const tasks = exists
      ? state.tasks.map((item) => (item.id === mapped.id ? mapped : item))
      : [...state.tasks, mapped];
    return { ...state, tasks };
  });
}

function handleTaskAndInteractionEvent(event: ProtocolEvent): boolean {
  if (event.type === "interaction.requested") {
    upsertInteraction(event.payload.interaction);
    return true;
  }
  if (
    event.type === "interaction.resolved" ||
    event.type === "interaction.cancelled"
  ) {
    removeInteraction(event.payload.interaction_id);
    return true;
  }
  if (event.type === "task.created" || event.type === "task.status_changed") {
    upsertRoleTask(event.payload.task);
    syncTreeNodes();
    return true;
  }
  if (event.type === "task.progress") {
    workspaceStore.setState((state) => ({
      ...state,
      tasks: state.tasks.map((task) =>
        task.taskCode === `@${event.payload.task_id}`
          ? {
              ...task,
              livePhase: event.payload.live_phase,
              currentStep: event.payload.current_step,
              maxRequests: event.payload.max_requests,
              toolCallsCount: event.payload.tool_calls_count,
              maxToolCalls: event.payload.max_tool_calls,
              ...(event.payload.active_tool?.tool_name
                ? { activeToolName: event.payload.active_tool.tool_name }
                : {}),
            }
          : task
      ),
    }));
    return true;
  }
  if (event.type === "workspace.locks_changed") {
    connectionStore.setState((state) => ({
      ...state,
      workspaceLocks: event.payload.locks,
    }));
    return true;
  }
  return false;
}

function applyBranchUpsert(branch: BranchView): void {
  if (branch.scope !== "main" || branch.role_name || branch.task_id) {
    return;
  }
  if (latestTreeView) {
    const existsInTree = latestTreeView.branches.some(
      (item) => item.conversation_id === branch.conversation_id
    );
    latestTreeView = {
      ...latestTreeView,
      branches: existsInTree
        ? latestTreeView.branches.map((item) =>
            item.conversation_id === branch.conversation_id ? branch : item
          )
        : [branch, ...latestTreeView.branches],
    };
  }
  const runningSet = new Set(connectionStore.state.runningConversationIds);
  const mapped = mapBranchToRoute(
    branch,
    runningSet,
    workspaceStore.state.activeRouteId
  );
  workspaceStore.setState((state) => {
    const exists = state.routes.some((route) => route.id === mapped.id);
    return {
      ...state,
      routes: exists
        ? state.routes.map((route) => (route.id === mapped.id ? mapped : route))
        : [mapped, ...state.routes],
    };
  });
  syncTreeNodes();
  if (mapped.workPath && mapped.workPath !== ".") {
    void loadWorkspaceFiles(mapped.id, mapped.workPath);
  }
}

function handleTreeAndContextEvent(event: ProtocolEvent): void {
  if (event.type === "context.snapshot") {
    const snap = event.payload.snapshot;
    connectionStore.setState((state) => ({
      ...state,
      rawContextByThread: {
        ...state.rawContextByThread,
        [snap.conversation_id]: snap,
      },
    }));
    const route = workspaceStore.state.routes.find(
      (item) => item.id === snap.conversation_id
    );
    const mapped = mapContextSnapshot(snap, route?.title ?? "当前会话");
    workspaceStore.setState((state) => ({
      ...state,
      contextByThread: {
        ...state.contextByThread,
        [snap.conversation_id]: mapped,
      },
    }));
    return;
  }
  if (event.type === "llm.pool_status") {
    connectionStore.setState((state) => ({
      ...state,
      poolStatus: event.payload.pool_status,
    }));
    return;
  }
  if (event.type === "artifact.updated") {
    const defaultRoute = workspaceStore.state.activeRouteId;
    const mapped = mapArtifactView(event.payload.artifact, defaultRoute);
    workspaceStore.setState((state) => {
      const exists = state.artifacts.some((item) => item.id === mapped.id);
      return {
        ...state,
        artifacts: exists
          ? state.artifacts.map((item) =>
              item.id === mapped.id ? mapped : item
            )
          : [mapped, ...state.artifacts],
      };
    });
    return;
  }
  if (
    event.type === "tree.branch_created" ||
    event.type === "tree.branch_updated"
  ) {
    applyBranchUpsert(event.payload.branch);
    return;
  }
  if (event.type === "tree.branch_switched") {
    const activeId = event.payload.active_conversation_id;
    if (latestTreeView) {
      latestTreeView = {
        ...latestTreeView,
        active_conversation_id: activeId,
      };
    }
    workspaceStore.setState((state) => ({
      ...state,
      activeRouteId: activeId,
      activeTaskId: null,
    }));
    syncTreeNodes();
    void loadConversationEntries(activeId);
    return;
  }
  if (event.type === "tree.entry_appended") {
    const { conversation_id: cid, entries } = event.payload;
    workspaceStore.setState((state) => {
      const prev = state.messagesByThread[cid] ?? [];
      const prevIds = new Set(prev.map((msg) => msg.id));
      const streamedRunIds = new Set(
        prev
          .filter((msg) => msg.runId && msg.sender !== "user")
          .map((msg) => msg.runId)
      );
      const appended = entries
        .filter(
          (entry) =>
            !(
              prevIds.has(entry.entry_id) ||
              (entry.run_id && streamedRunIds.has(entry.run_id))
            )
        )
        .map((entry) => mapEntryToChatMessage(entry, cid));
      if (appended.length === 0) {
        return state;
      }
      return {
        ...state,
        messagesByThread: {
          ...state.messagesByThread,
          [cid]: [...prev, ...appended],
        },
      };
    });
  }
}

export function handleProtocolEvent(event: ProtocolEvent): void {
  connectionStore.setState((state) => ({
    ...state,
    latestSeq: Math.max(state.latestSeq, event.seq),
  }));

  if (event.type === "runtime.ready") {
    setSubscriptionId(event.payload.subscription_id);
    connectionStore.setState((state) => ({
      ...state,
      subscriptionId: event.payload.subscription_id,
      modelName: event.payload.model_name,
      trustedLocal: event.payload.trusted_local,
    }));
    return;
  }

  if (event.type === "stream.resync_required") {
    void refreshRuntimeSnapshot();
    return;
  }

  if (handlePartEvent(event) || handleRunLifecycleEvent(event)) {
    return;
  }

  if (handleTaskAndInteractionEvent(event)) {
    return;
  }

  handleTreeAndContextEvent(event);
}

export async function loadConversationEntries(
  conversationId: string,
  threadId = conversationId,
  roleHint?: RoleName
): Promise<void> {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  try {
    const page = await client.getTurns(conversationId, 100);
    const messages = page.turns.flatMap((turn, index) =>
      mapTurnToChatMessages(turn, threadId, index, roleHint)
    );
    workspaceStore.setState((state) => ({
      ...state,
      messagesByThread: {
        ...state.messagesByThread,
        [threadId]: messages,
      },
    }));
  } catch {
    // Keep existing thread messages if entry fetch fails.
  }
}

function hydrateWorkspaceFromSnapshot(
  snapshot: RuntimeSnapshotView
): string | null {
  latestTreeView = snapshot.tree;
  const runningIds = snapshot.tree.branches
    .filter((branch) => branch.is_running)
    .map((branch) => branch.conversation_id);
  const runningSet = new Set(runningIds);
  const mainBranches = snapshot.tree.branches.filter(
    (branch) => branch.scope === "main" && !branch.role_name && !branch.task_id
  );
  const activeRouteId =
    snapshot.tree.active_conversation_id ??
    mainBranches[0]?.conversation_id ??
    null;

  const routes = mainBranches.map((branch) =>
    mapBranchToRoute(branch, runningSet, activeRouteId)
  );
  const tasks = snapshot.tasks.map((task) =>
    mapTaskViewToRoleTask(task, snapshot.workspace_locks)
  );
  const treeNodes = mapTreeToNodes(
    snapshot.tree,
    tasks,
    runningSet,
    activeRouteId
  );

  const contextByThread: Record<
    string,
    ReturnType<typeof mapContextSnapshot>
  > = {};
  for (const [cid, ctx] of Object.entries(snapshot.context_snapshots)) {
    const routeTitle =
      routes.find((route) => route.id === cid)?.title ?? "当前会话";
    contextByThread[cid] = mapContextSnapshot(ctx, routeTitle);
  }

  connectionStore.setState((state) => ({
    ...state,
    modelName: snapshot.model_name,
    trustedLocal: snapshot.trusted_local,
    latestSeq: snapshot.latest_seq,
    poolStatus: snapshot.pool_status,
    runningConversationIds: runningIds,
    workspaceLocks: snapshot.workspace_locks,
    rawContextByThread: snapshot.context_snapshots,
  }));

  setPendingInteractions(snapshot.pending_interactions);

  workspaceStore.setState((state) => ({
    ...state,
    routes: routes.length > 0 ? routes : state.routes,
    tasks,
    treeNodes,
    contextByThread:
      Object.keys(contextByThread).length > 0
        ? { ...state.contextByThread, ...contextByThread }
        : state.contextByThread,
    activeRouteId: activeRouteId ?? state.activeRouteId,
    activeTaskId: null,
  }));

  const activeRoute = routes.find((route) => route.id === activeRouteId);
  if (activeRoute && activeRoute.workPath !== ".") {
    void loadWorkspaceFiles(activeRoute.id, activeRoute.workPath);
  }

  return activeRouteId;
}

export async function refreshCapabilities(
  conversationId?: string
): Promise<void> {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  const [skillsRes, mcpsRes] = await Promise.allSettled([
    client.listSkills(
      conversationId ? { conversation_id: conversationId } : undefined
    ),
    client.listMcps(),
  ]);
  connectionStore.setState((state) => ({
    ...state,
    ...(skillsRes.status === "fulfilled"
      ? {
          skills: skillsRes.value.skills,
          skippedSkills: skillsRes.value.skipped,
        }
      : {}),
    ...(mcpsRes.status === "fulfilled"
      ? { mcpServers: mcpsRes.value.servers }
      : {}),
  }));
}

export async function refreshRuntimeSnapshot(): Promise<void> {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  const snapshot = await client.getRuntimeSnapshot();
  const activeId = hydrateWorkspaceFromSnapshot(snapshot);
  if (activeId) {
    await loadConversationEntries(activeId);
  }
}

export async function connectRuntime(override?: {
  baseUrl?: string;
  token?: string;
}): Promise<boolean> {
  connectGeneration += 1;
  const gen = connectGeneration;
  const baseUrl = override?.baseUrl ?? connectionStore.state.baseUrl;
  const token = override?.token ?? connectionStore.state.token;
  saveConfig(baseUrl, token);

  closeSseStream?.();
  closeSseStream = null;
  setProtocolClient(null);
  setSubscriptionId(null);

  connectionStore.setState((state) => ({
    ...state,
    baseUrl,
    token,
    status: "connecting",
    error: null,
  }));

  try {
    const client = new GuiProtocolClient({ baseUrl, authToken: token });
    const snapshot = await client.getRuntimeSnapshot();
    if (gen !== connectGeneration) {
      return false;
    }
    setProtocolClient(client);

    const activeId = hydrateWorkspaceFromSnapshot(snapshot);
    const artifactTasks: Promise<unknown>[] = [
      client.listArtifacts().then((items) => {
        if (gen !== connectGeneration) {
          return;
        }
        const fallbackRoute = activeId ?? workspaceStore.state.activeRouteId;
        const mapped = items.map((item) =>
          mapArtifactView(item, fallbackRoute)
        );
        workspaceStore.setState((state) => ({
          ...state,
          artifacts: mapped,
        }));
      }),
    ];
    if (activeId) {
      artifactTasks.push(loadConversationEntries(activeId));
    }
    artifactTasks.push(refreshCapabilities(activeId ?? undefined));
    await Promise.allSettled(artifactTasks);
    if (gen !== connectGeneration) {
      return false;
    }

    closeSseStream = openProtocolSse({
      baseUrl,
      authToken: token,
      afterSeq: snapshot.latest_seq,
      onEvent: handleProtocolEvent,
      onReconnect: () => {
        connectionStore.setState((state) => ({
          ...state,
          status: "connected",
          error: null,
        }));
      },
      onDisconnect: (err) => {
        if (connectionStore.state.status === "disconnected") {
          return;
        }
        connectionStore.setState((state) => ({
          ...state,
          status: "error",
          error: err ? err.message : "连接已断开",
        }));
      },
    });

    connectionStore.setState((state) => ({
      ...state,
      status: "connected",
      error: null,
    }));
    return true;
  } catch (error) {
    setProtocolClient(null);
    const message = error instanceof Error ? error.message : "连接后端服务失败";
    connectionStore.setState((state) => ({
      ...state,
      status: "error",
      error: message,
    }));
    return false;
  }
}

export async function discoverAndConnectRuntime(): Promise<boolean> {
  if (typeof window === "undefined") {
    return false;
  }
  try {
    const response = await fetch("/__agent/runtime");
    if (response.ok) {
      const data = (await response.json()) as {
        found?: boolean;
        udsPath?: string | null;
        httpUrl?: string;
        authToken?: string;
        pid?: number | null;
      };
      if (data.found && data.authToken) {
        connectionStore.setState((state) => ({
          ...state,
          discoveredRuntime: {
            udsPath: data.udsPath ?? null,
            httpUrl: data.httpUrl ?? "",
            pid: data.pid ?? null,
          },
        }));
        return connectRuntime({
          baseUrl: "",
          token: data.authToken,
        });
      }
    }
  } catch {
    // Discovery endpoint unavailable; fall back to manual config if present.
  }

  if (connectionStore.state.token.trim()) {
    return connectRuntime();
  }
  return false;
}

export function disconnectRuntime(): void {
  connectGeneration += 1;
  latestTreeView = null;
  closeSseStream?.();
  closeSseStream = null;
  setProtocolClient(null);
  setSubscriptionId(null);
  clearInteractions();
  workspaceStore.setState((state) => ({
    ...state,
    routes: [],
    tasks: [],
    artifacts: [],
    messagesByThread: {},
    treeNodes: [],
    contextByThread: {},
    filesByRoute: {},
    activeRouteId: "",
    activeTaskId: null,
    selectedTreeNodeId: "",
    selectedFilePath: null,
  }));
  connectionStore.setState((state) => ({
    ...state,
    status: "disconnected",
    error: null,
    mcpServers: [],
    subscriptionId: null,
    runningConversationIds: [],
    skills: [],
    skippedSkills: [],
    workspaceLocks: [],
    rawContextByThread: {},
  }));
}
