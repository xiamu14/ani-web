import { useSelector } from "@tanstack/react-store";
import {
  ScrollArea,
  ScrollAreaContent,
  ScrollAreaScrollbar,
  ScrollAreaThumb,
  ScrollAreaViewport,
} from "@ui-sdk/react/scroll-area";
import type { ChatMessage } from "@/schemas/workspace.ts";
import { interactionStore } from "@/store/interactions.ts";
import { workspaceStore } from "@/store/workspace.ts";
import { InteractionBanner } from "./interaction-banner.tsx";
import { MessageItem } from "./message-item.tsx";

const EMPTY_MESSAGES: ChatMessage[] = [];

export function MessageList() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const activeTaskId = useSelector(workspaceStore, (s) => s.activeTaskId);
  const threadId = activeTaskId ?? activeRouteId;
  const rawMessages = useSelector(
    workspaceStore,
    (s) => s.messagesByThread[threadId] ?? EMPTY_MESSAGES
  );
  const pendingInteractions = useSelector(interactionStore, (s) => s.pending);

  const pendingRunIds = new Set(
    pendingInteractions
      .map((item) => item.run_id)
      .filter((id): id is string => Boolean(id))
  );

  const messages = rawMessages.filter((msg) => {
    const isEmptyStreaming =
      Boolean(msg.streaming) &&
      !msg.content &&
      !msg.thinking &&
      !(msg.toolTraces && msg.toolTraces.length > 0) &&
      !(msg.steps && msg.steps.length > 0);
    if (isEmptyStreaming && msg.runId && pendingRunIds.has(msg.runId)) {
      return false;
    }
    return true;
  });

  return (
    <ScrollArea className="relative flex min-h-0 flex-1 flex-col" stickToEnd>
      <ScrollAreaViewport
        aria-label="对话记录"
        className="scrollbar-hide h-full w-full overflow-y-auto px-4 py-6 sm:px-6"
        role="region"
      >
        <ScrollAreaContent className="mx-auto flex w-full max-w-3xl flex-col gap-5">
          {messages.length === 0 && pendingInteractions.length === 0 ? (
            <div className="border-border-subtle text-text-muted rounded-xl border border-dashed py-12 text-center text-sm">
              暂无消息
            </div>
          ) : (
            messages.map((message) => (
              <MessageItem key={message.id} message={message} />
            ))
          )}
          <InteractionBanner />
        </ScrollAreaContent>
      </ScrollAreaViewport>
      <ScrollAreaScrollbar
        className="absolute inset-y-2 right-1 w-1.5 opacity-0 transition-opacity data-visible:opacity-100"
        orientation="vertical"
      >
        <ScrollAreaThumb className="bg-border w-full rounded-full" />
      </ScrollAreaScrollbar>
    </ScrollArea>
  );
}
