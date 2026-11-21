import { Store } from "@tanstack/react-store";
import { nanoid } from "nanoid";
import { getProtocolClient } from "@/protocol/runtime-client";
import type {
  InteractionRespondRequest,
  InteractionView,
} from "@/protocol/types";
import type { ChatMessage } from "@/schemas/workspace";
import { workspaceStore } from "@/store/workspace";

export interface InteractionStoreState {
  errorById: Record<string, string | null>;
  pending: InteractionView[];
  submittingIds: Record<string, boolean>;
}

export const interactionStore = new Store<InteractionStoreState>({
  pending: [],
  submittingIds: {},
  errorById: {},
});

export function setPendingInteractions(
  items: readonly InteractionView[]
): void {
  interactionStore.setState((state) => ({
    ...state,
    pending: [...items],
  }));
}

export function upsertInteraction(item: InteractionView): void {
  interactionStore.setState((state) => {
    const exists = state.pending.some(
      (existing) => existing.interaction_id === item.interaction_id
    );
    const pending = exists
      ? state.pending.map((existing) =>
          existing.interaction_id === item.interaction_id ? item : existing
        )
      : [...state.pending, item];
    return { ...state, pending };
  });
}

export function removeInteraction(interactionId: string): void {
  interactionStore.setState((state) => {
    const nextSubmitting = { ...state.submittingIds };
    delete nextSubmitting[interactionId];
    const nextErrors = { ...state.errorById };
    delete nextErrors[interactionId];
    return {
      ...state,
      pending: state.pending.filter(
        (item) => item.interaction_id !== interactionId
      ),
      submittingIds: nextSubmitting,
      errorById: nextErrors,
    };
  });
}

export function prioritizeInteraction(interactionId: string): void {
  interactionStore.setState((state) => {
    const index = state.pending.findIndex(
      (item) => item.interaction_id === interactionId
    );
    if (index <= 0) {
      return state;
    }
    const target = state.pending[index];
    if (!target) {
      return state;
    }
    const rest = state.pending.filter(
      (item) => item.interaction_id !== interactionId
    );
    return {
      ...state,
      pending: [target, ...rest],
    };
  });
}

export function clearInteractions(): void {
  interactionStore.setState(() => ({
    pending: [],
    submittingIds: {},
    errorById: {},
  }));
}

function formatAskUserSummary(
  payload: Extract<InteractionRespondRequest, { kind: "ask_user" }>
): string {
  if (payload.cancelled) {
    return "已跳过问题确认";
  }
  const parts = payload.answers.map((ans) => {
    const picks = [...(ans.selected ?? [])];
    if (ans.custom_answer) {
      picks.push(ans.custom_answer);
    }
    const text = picks.length > 0 ? picks.join("、") : "未选择";
    return `${ans.header}：${text}`;
  });
  return parts.join("；") || "已提交选择";
}

export async function submitInteractionResponse(
  interactionId: string,
  payload: InteractionRespondRequest
): Promise<boolean> {
  const client = getProtocolClient();
  if (!client) {
    interactionStore.setState((state) => ({
      ...state,
      errorById: {
        ...state.errorById,
        [interactionId]: "未连接到后端服务",
      },
    }));
    return false;
  }

  const target = interactionStore.state.pending.find(
    (item) => item.interaction_id === interactionId
  );

  interactionStore.setState((state) => ({
    ...state,
    submittingIds: { ...state.submittingIds, [interactionId]: true },
    errorById: { ...state.errorById, [interactionId]: null },
  }));

  try {
    await client.respondInteraction(interactionId, payload);
    removeInteraction(interactionId);

    if (target && payload.kind === "ask_user") {
      const threadId = target.conversation_id;
      const summaryMsg: ChatMessage = {
        id: `ans-${nanoid(8)}`,
        threadId,
        sender: "system",
        senderLabel: "已选择",
        content: formatAskUserSummary(payload),
        createdAt: "刚刚",
      };
      workspaceStore.setState((state) => ({
        ...state,
        messagesByThread: {
          ...state.messagesByThread,
          [threadId]: [...(state.messagesByThread[threadId] ?? []), summaryMsg],
        },
      }));
    }
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : "提交交互响应失败";
    interactionStore.setState((state) => ({
      ...state,
      submittingIds: { ...state.submittingIds, [interactionId]: false },
      errorById: { ...state.errorById, [interactionId]: message },
    }));
    return false;
  }
}
