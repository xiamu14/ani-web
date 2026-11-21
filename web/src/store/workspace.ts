import { createStore } from "@tanstack/react-store";
import { nanoid } from "nanoid";
import { mapBranchToRoute, mapTurnToChatMessages } from "@/protocol/mappers";
import {
  getProtocolClient,
  getSubscriptionId,
} from "@/protocol/runtime-client";
import type {
  ChatMessage,
  ComposerReference,
  ConversationTreeNode,
  DashboardTab,
  RoleName,
  WorkspaceFileNode,
  WorkspaceSnapshot,
} from "@/schemas/workspace.ts";
import { chatMessageSchema } from "@/schemas/workspace.ts";
import { getStagedFiles } from "@/store/composer";

const LEADING_AT_REGEX = /^@/u;

function syncThreadEntries(
  conversationId: string,
  threadId = conversationId,
  roleHint?: RoleName
): void {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  void client
    .getTurns(conversationId, 100)
    .then((page) => {
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
    })
    .catch(() => {
      // Keep existing thread messages if entry fetch fails.
    });
}

export interface WorkspaceState extends WorkspaceSnapshot {
  activeRouteId: string;
  activeTaskId: string | null;
  dashboardTab: DashboardTab;
  leftPanelOpen: boolean;
  rightPanelOpen: boolean;
  selectedFilePath: string | null;
  selectedTreeNodeId: string;
}

function createInitialState(): WorkspaceState {
  return {
    routes: [],
    tasks: [],
    artifacts: [],
    messagesByThread: {},
    treeNodes: [],
    contextByThread: {},
    filesByRoute: {},
    activeRouteId: "",
    activeTaskId: null,
    dashboardTab: "context",
    selectedTreeNodeId: "",
    selectedFilePath: null,
    leftPanelOpen: true,
    rightPanelOpen: true,
  };
}

export const workspaceStore = createStore<WorkspaceState>(createInitialState());

export function resetWorkspaceStore(): void {
  workspaceStore.setState(() => createInitialState());
}

function findTreeNodeByRef(
  nodes: readonly ConversationTreeNode[],
  refId: string
): ConversationTreeNode | null {
  for (const node of nodes) {
    if (node.refId === refId) {
      return node;
    }
    if (node.children) {
      const found = findTreeNodeByRef(node.children, refId);
      if (found) {
        return found;
      }
    }
  }
  return null;
}

export function selectRoute(routeId: string): void {
  workspaceStore.setState((state) => {
    const matchedNode = findTreeNodeByRef(state.treeNodes, routeId);
    const firstFile = state.filesByRoute[routeId]?.[0]?.path ?? null;
    return {
      ...state,
      activeRouteId: routeId,
      activeTaskId: null,
      selectedTreeNodeId: matchedNode?.id ?? state.selectedTreeNodeId,
      selectedFilePath: firstFile,
    };
  });

  const client = getProtocolClient();
  if (client) {
    const subId = getSubscriptionId();
    if (subId) {
      void client.updateSubscriptions(subId, []);
    }
    void client
      .switchConversation(routeId)
      .then(() => syncThreadEntries(routeId))
      .catch(() => {
        // Keep current view if branch cannot be switched.
      });
  }
}

export function selectTask(taskId: string): void {
  let selectedTask: (typeof workspaceStore.state.tasks)[number] | undefined;
  workspaceStore.setState((state) => {
    const task = state.tasks.find((item) => item.id === taskId);
    if (!task) {
      return state;
    }
    selectedTask = task;
    const matchedNode = findTreeNodeByRef(state.treeNodes, taskId);
    return {
      ...state,
      activeRouteId: task.routeId,
      activeTaskId: taskId,
      selectedTreeNodeId: matchedNode?.id ?? state.selectedTreeNodeId,
    };
  });

  const client = getProtocolClient();
  if (client && selectedTask) {
    const rawTaskId = selectedTask.taskCode.replace(LEADING_AT_REGEX, "");
    const subId = getSubscriptionId();
    if (subId) {
      void client.updateSubscriptions(subId, [rawTaskId]);
    }
    if (selectedTask.roleConversationId) {
      syncThreadEntries(
        selectedTask.roleConversationId,
        selectedTask.id,
        selectedTask.role
      );
    }
  }
}

export function returnToRoute(): void {
  workspaceStore.setState((state) => {
    const matchedNode = findTreeNodeByRef(state.treeNodes, state.activeRouteId);
    return {
      ...state,
      activeTaskId: null,
      selectedTreeNodeId: matchedNode?.id ?? state.selectedTreeNodeId,
    };
  });

  const client = getProtocolClient();
  const subId = getSubscriptionId();
  if (client && subId) {
    void client.updateSubscriptions(subId, []);
  }
}

export function setDashboardTab(dashboardTab: DashboardTab): void {
  workspaceStore.setState((state) =>
    state.dashboardTab === dashboardTab ? state : { ...state, dashboardTab }
  );
}

export function selectTreeNode(selectedTreeNodeId: string): void {
  workspaceStore.setState((state) =>
    state.selectedTreeNodeId === selectedTreeNodeId
      ? state
      : { ...state, selectedTreeNodeId }
  );
}

export function activateTreeNode(
  kind: ConversationTreeNode["kind"],
  refId: string
): void {
  if (kind === "route") {
    selectRoute(refId);
  } else if (kind === "task") {
    selectTask(refId);
  }
}

export function selectFile(selectedFilePath: string | null): void {
  workspaceStore.setState((state) =>
    state.selectedFilePath === selectedFilePath
      ? state
      : { ...state, selectedFilePath }
  );
}

export function toggleLeftPanel(): void {
  workspaceStore.setState((state) => ({
    ...state,
    leftPanelOpen: !state.leftPanelOpen,
  }));
}

export function toggleRightPanel(): void {
  workspaceStore.setState((state) => ({
    ...state,
    rightPanelOpen: !state.rightPanelOpen,
  }));
}

export type DeliveryMode = "auto" | "steer" | "queue" | "interrupt_and_send";

function buildMessageTextWithRefs(
  content: string,
  references: readonly ComposerReference[]
): string {
  const inlineRefs = references
    .filter((ref) => ref.kind !== "attachment")
    .map((ref) => {
      if (ref.kind === "file") {
        return `@${ref.path}`;
      }
      if (ref.kind === "task") {
        return `@${ref.role}(${ref.taskCode})`;
      }
      return ref.artifactCode;
    });
  if (inlineRefs.length === 0) {
    return content;
  }
  return [content, inlineRefs.join(" ")].filter(Boolean).join("\n");
}

export function sendMessage(
  rawContent: string,
  references: readonly ComposerReference[] = [],
  deliveryMode: DeliveryMode = "auto"
): void {
  const content = rawContent.trim();
  if (!content && references.length === 0) {
    return;
  }

  const client = getProtocolClient();
  if (!client) {
    return;
  }

  const { activeRouteId, activeTaskId, tasks } = workspaceStore.state;
  const threadId = activeTaskId ?? activeRouteId;
  const message: ChatMessage = chatMessageSchema.assert({
    id: `msg-${nanoid(8)}`,
    threadId,
    sender: "user",
    senderLabel: "你",
    content,
    createdAt: "刚刚",
    ...(references.length > 0 ? { references: [...references] } : {}),
  });

  workspaceStore.setState((prevState) => {
    const prev = prevState.messagesByThread[threadId] ?? [];
    return {
      ...prevState,
      messagesByThread: {
        ...prevState.messagesByThread,
        [threadId]: [...prev, message],
      },
    };
  });

  const activeTask = activeTaskId
    ? tasks.find((item) => item.id === activeTaskId)
    : undefined;
  const textPayload = buildMessageTextWithRefs(content, references);

  if (activeTask) {
    const rawTaskId = activeTask.taskCode.replace(LEADING_AT_REGEX, "");
    void client.steerTask(
      activeTask.routeId,
      rawTaskId,
      textPayload || "请继续处理"
    );
    return;
  }

  const stagedFiles = getStagedFiles(references);
  const routeId = activeRouteId;
  void (async () => {
    try {
      let attachmentIds: string[] | undefined;
      if (stagedFiles.length > 0) {
        const uploaded = await client.uploadAttachments(stagedFiles);
        attachmentIds = uploaded.map((item) => item.attachment_id);
      }
      const receipt = await client.sendMessage(routeId, {
        client_message_id: message.id,
        text: textPayload,
        delivery_mode: deliveryMode,
        ...(attachmentIds && attachmentIds.length > 0
          ? { attachment_ids: attachmentIds }
          : {}),
      });
      const targetThreadId = receipt.resolved_conversation_id || threadId;
      workspaceStore.setState((prevState) => {
        const prev = prevState.messagesByThread[targetThreadId] ?? [];
        const placeholderId = `run-${receipt.run_id}`;
        const alreadyExists = prev.some(
          (item) => item.id === placeholderId || item.runId === receipt.run_id
        );
        if (alreadyExists) {
          return prevState;
        }
        const placeholder: ChatMessage = {
          id: placeholderId,
          threadId: targetThreadId,
          sender: "router",
          senderLabel: "主路由",
          content: "",
          createdAt: "刚刚",
          runId: receipt.run_id,
          streaming: true,
        };
        return {
          ...prevState,
          messagesByThread: {
            ...prevState.messagesByThread,
            [targetThreadId]: [...prev, placeholder],
          },
        };
      });
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : "发送消息失败";
      workspaceStore.setState((prevState) => {
        const prev = prevState.messagesByThread[threadId] ?? [];
        const notice: ChatMessage = {
          id: `err-${nanoid(6)}`,
          threadId,
          sender: "system",
          senderLabel: "系统",
          content: "",
          error: errMsg,
          createdAt: "刚刚",
        };
        return {
          ...prevState,
          messagesByThread: {
            ...prevState.messagesByThread,
            [threadId]: [...prev, notice],
          },
        };
      });
    }
  })();
}

export function createMockRoute(): void {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  void client.createConversation({ switch_to: true }).then((branch) => {
    const mapped = mapBranchToRoute(
      branch,
      new Set<string>(),
      branch.conversation_id
    );
    workspaceStore.setState((state) => {
      const exists = state.routes.some((item) => item.id === mapped.id);
      return {
        ...state,
        routes: exists ? state.routes : [mapped, ...state.routes],
        activeRouteId: mapped.id,
        activeTaskId: null,
      };
    });
  });
}

export async function interruptActiveRun(): Promise<void> {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  const routeId = workspaceStore.state.activeRouteId;
  await client.interruptConversation(routeId);
}

export async function forkActiveRoute(
  mode: "full_fork" | "handoff"
): Promise<void> {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  const routeId = workspaceStore.state.activeRouteId;
  await client.forkConversation(routeId, {
    mode,
    switch_to: true,
  });
}

export async function loadWorkspaceFiles(
  routeId: string,
  workPath: string
): Promise<void> {
  if (
    !(routeId && workPath) ||
    workPath === "." ||
    typeof window === "undefined"
  ) {
    return;
  }
  try {
    const response = await fetch(
      `/__agent/files?path=${encodeURIComponent(workPath)}`
    );
    if (!response.ok) {
      return;
    }
    const data = (await response.json()) as { files?: WorkspaceFileNode[] };
    const files = data.files ?? [];
    workspaceStore.setState((state) => ({
      ...state,
      filesByRoute: {
        ...state.filesByRoute,
        [routeId]: files,
      },
    }));
  } catch {
    // Ignore file tree fetch errors.
  }
}

export async function cancelRoleTask(taskId: string): Promise<void> {
  const client = getProtocolClient();
  if (!client) {
    return;
  }
  const task = workspaceStore.state.tasks.find((item) => item.id === taskId);
  if (!task) {
    return;
  }
  const rawTaskId = task.taskCode.replace(LEADING_AT_REGEX, "");
  await client.cancelTask(task.routeId, rawTaskId);
}
