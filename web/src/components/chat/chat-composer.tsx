import {
  AtIcon,
  Attachment01Icon,
  Folder01Icon,
  SentIcon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import type { FileInputValueChangeDetails } from "@ui-sdk/react/file-input";
import {
  createFileInputController,
  FileInput,
  FileInputDropzone,
  FileInputTrigger,
} from "@ui-sdk/react/file-input";
import { Popover, PopoverContent, PopoverTrigger } from "@ui-sdk/react/popover";
import type { ChangeEvent, DragEvent, KeyboardEvent } from "react";
import { useCallback, useRef, useState } from "react";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import { hasInternalDragRef, readDragRef } from "@/lib/drag-ref.ts";
import { pathBasename } from "@/lib/format.ts";
import type { DraggingRefPreview } from "@/store/composer.ts";
import {
  addDragPayloadReference,
  addFileAttachments,
  clearComposerReferences,
  composerStore,
  removeComposerReference,
  setDraggingRef,
} from "@/store/composer.ts";
import { connectionStore } from "@/store/connection.ts";
import { interactionStore } from "@/store/interactions.ts";
import type { DeliveryMode } from "@/store/workspace.ts";
import { sendMessage, workspaceStore } from "@/store/workspace.ts";
import { ReferenceChip } from "./reference-chip.tsx";
import { ToolsPopover } from "./tools-popover.tsx";

const MENTION_ITEMS = [
  { token: "@coder ", label: "编码助手", hint: "修改代码" },
  { token: "@reviewer ", label: "审查助手", hint: "只读检查" },
  { token: "@tester ", label: "测试助手", hint: "运行测试" },
  { token: "@researcher ", label: "调研助手", hint: "检索资料" },
] as const;

function MentionOption({
  hint,
  label,
  onSelect,
  token,
}: {
  readonly token: string;
  readonly label: string;
  readonly hint: string;
  readonly onSelect: (token: string) => void;
}) {
  const handleClick = useCallback(() => onSelect(token), [onSelect, token]);

  return (
    <button
      className="hover:bg-surface-hover flex cursor-pointer items-center justify-between rounded-lg px-2 py-1.5 text-left text-xs transition-colors"
      onClick={handleClick}
      type="button"
    >
      <span className="text-text font-medium">{label}</span>
      <span className="text-text-muted">{hint}</span>
    </button>
  );
}

function isInternalDrag(dataTransfer: DataTransfer | null): boolean {
  return (
    hasInternalDragRef(dataTransfer) || composerStore.state.draggingRef !== null
  );
}

function formatDropLabel(
  draggingRef: DraggingRefPreview | null
): string | null {
  if (!draggingRef) {
    return null;
  }
  if (draggingRef.kind === "artifact") {
    return `拖放以引用产物 · ${draggingRef.label}`;
  }
  if (draggingRef.kind === "task") {
    return `拖放以引用任务 · ${draggingRef.label}`;
  }
  return `拖放以引用文件 · ${draggingRef.label}`;
}

export function ChatComposer() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const activeTaskId = useSelector(workspaceStore, (s) => s.activeTaskId);
  const routes = useSelector(workspaceStore, (s) => s.routes);
  const tasks = useSelector(workspaceStore, (s) => s.tasks);
  const references = useSelector(composerStore, (s) => s.references);
  const draggingRef = useSelector(composerStore, (s) => s.draggingRef);
  const runningConversationIds = useSelector(
    connectionStore,
    (s) => s.runningConversationIds
  );
  const connectionStatus = useSelector(connectionStore, (s) => s.status);
  const pendingInteractions = useSelector(interactionStore, (s) => s.pending);
  const isConnected = connectionStatus === "connected";
  const routerBlocked = pendingInteractions.some(
    (item) => item.conversation_id === activeRouteId
  );

  const [draft, setDraft] = useState("");
  const [mentionOpen, setMentionOpen] = useState(false);
  const [deliveryMode, setDeliveryMode] = useState<DeliveryMode>("auto");
  const [internalOver, setInternalOver] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [fileController] = useState(createFileInputController);

  const isRunning = runningConversationIds.includes(activeRouteId);

  const handleDeliveryChange = useCallback(
    (event: ChangeEvent<HTMLSelectElement>) => {
      setDeliveryMode(event.target.value as DeliveryMode);
    },
    []
  );

  const route = routes.find((item) => item.id === activeRouteId);
  const activeTask = activeTaskId
    ? tasks.find((item) => item.id === activeTaskId)
    : undefined;
  const workPath = activeTask?.workPath ?? route?.workPath ?? "";
  const canSend =
    isConnected &&
    !routerBlocked &&
    (draft.trim().length > 0 || references.length > 0);

  let composerPlaceholder = "未连接";
  if (isConnected) {
    if (routerBlocked) {
      composerPlaceholder = "请先完成确认";
    } else {
      composerPlaceholder = activeTask
        ? `向 ${activeTask.taskCode} 发送…`
        : "输入指令…";
    }
  }

  const getPasteTarget = useCallback(() => textareaRef.current, []);

  const handleFilesChange = useCallback(
    (_files: File[], details: FileInputValueChangeDetails) => {
      if (details.added.length > 0) {
        addFileAttachments(details.added);
        details.cancel();
      }
    },
    []
  );

  const handleDraftChange = useCallback(
    (event: ChangeEvent<HTMLTextAreaElement>) => {
      setDraft(event.target.value);
    },
    []
  );

  const handleSend = useCallback(() => {
    const currentRefs = composerStore.state.references;
    if (!(draft.trim() || currentRefs.length > 0)) {
      return;
    }
    sendMessage(draft, currentRefs, deliveryMode);
    setDraft("");
    clearComposerReferences();
  }, [deliveryMode, draft]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (
        event.key === "Enter" &&
        !event.shiftKey &&
        !event.nativeEvent.isComposing
      ) {
        event.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  const insertMention = useCallback((token: string) => {
    setDraft((prev) => (prev ? `${prev.trimEnd()} ${token}` : token));
    setMentionOpen(false);
  }, []);

  const handleInternalDragEnter = useCallback(
    (event: DragEvent<HTMLElement>) => {
      if (!isInternalDrag(event.dataTransfer)) {
        return;
      }
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      setInternalOver(true);
    },
    []
  );

  const handleInternalDragOver = useCallback(
    (event: DragEvent<HTMLElement>) => {
      if (!isInternalDrag(event.dataTransfer)) {
        return;
      }
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      if (!internalOver) {
        setInternalOver(true);
      }
    },
    [internalOver]
  );

  const handleInternalDragLeave = useCallback(
    (event: DragEvent<HTMLElement>) => {
      const nextTarget = event.relatedTarget as Node | null;
      if (!event.currentTarget.contains(nextTarget)) {
        setInternalOver(false);
      }
    },
    []
  );

  const handleInternalDrop = useCallback((event: DragEvent<HTMLElement>) => {
    if (!isInternalDrag(event.dataTransfer)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const payload =
      readDragRef(event.dataTransfer) ??
      composerStore.state.draggingRef?.payload ??
      null;
    if (payload) {
      addDragPayloadReference(payload);
    }
    setInternalOver(false);
    setDraggingRef(null);
  }, []);

  const dropLabel = formatDropLabel(draggingRef);

  return (
    <div className="border-border-subtle bg-stage/95 shrink-0 border-t px-4 py-3.5 backdrop-blur-sm sm:px-6">
      <FileInput
        controller={fileController}
        multiple
        onValueChange={handleFilesChange}
      >
        <FileInputDropzone
          className="group relative mx-auto w-full max-w-3xl"
          onDragEnter={handleInternalDragEnter}
          onDragLeave={handleInternalDragLeave}
          onDragOver={handleInternalDragOver}
          onDropCapture={handleInternalDrop}
          pasteTarget={getPasteTarget}
        >
          <div
            className={cn(
              "border-border bg-surface focus-within:border-accent/60 flex flex-col gap-2 rounded-2xl border p-3 shadow-xs transition-colors",
              "group-data-dragging:border-accent group-data-dragging:bg-accent-soft/30",
              internalOver ? "border-accent bg-accent-soft/30" : null,
              draggingRef && !internalOver
                ? "border-accent/50 border-dashed"
                : null
            )}
          >
            {dropLabel ? (
              <div className="bg-accent-soft text-accent-fg flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium">
                <span>{dropLabel}</span>
              </div>
            ) : null}

            <div className="bg-accent-soft text-accent-fg hidden items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium group-data-dragging:flex">
              <span>松开以添加附件文件</span>
            </div>

            {references.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {references.map((reference) => (
                  <ReferenceChip
                    key={reference.id}
                    onRemove={removeComposerReference}
                    reference={reference}
                    tone="surface"
                  />
                ))}
              </div>
            ) : null}

            <textarea
              aria-label="对话输入框"
              className="text-text placeholder:text-text-muted max-h-36 min-h-13 w-full resize-none bg-transparent text-sm leading-relaxed outline-none"
              disabled={!isConnected || routerBlocked}
              onChange={handleDraftChange}
              onKeyDown={handleKeyDown}
              placeholder={composerPlaceholder}
              ref={textareaRef}
              rows={2}
              value={draft}
            />

            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1.5">
                <FileInputTrigger
                  render={
                    <Button size="xs" variant="subtle">
                      <Icon icon={Attachment01Icon} size="xs" />
                      <span>附件</span>
                    </Button>
                  }
                />

                <Popover onOpenChange={setMentionOpen} open={mentionOpen}>
                  <PopoverTrigger
                    render={
                      <Button size="xs" variant="subtle">
                        <Icon icon={AtIcon} size="xs" />
                        <span>提及</span>
                      </Button>
                    }
                  />
                  <PopoverContent
                    align="start"
                    className="border-border bg-surface w-56 rounded-xl border p-1.5 shadow-lg"
                    side="top"
                    sideOffset={8}
                  >
                    <div className="text-text-muted px-2 py-1 text-[11px] font-semibold">
                      选择角色或任务
                    </div>
                    <div className="flex flex-col gap-0.5">
                      {MENTION_ITEMS.map((item) => (
                        <MentionOption
                          hint={item.hint}
                          key={item.token}
                          label={item.label}
                          onSelect={insertMention}
                          token={item.token}
                        />
                      ))}
                    </div>
                  </PopoverContent>
                </Popover>

                <ToolsPopover onInsertCommand={insertMention} />

                {workPath ? (
                  <span className="text-text-muted bg-surface-subtle inline-flex items-center gap-1 rounded-md px-2 py-1 font-mono text-[11px]">
                    <Icon icon={Folder01Icon} size="xs" />
                    <span>{pathBasename(workPath)}</span>
                  </span>
                ) : null}

                {isRunning && !activeTask ? (
                  <select
                    aria-label="发送方式"
                    className="border-border-subtle bg-surface-subtle text-text-secondary h-7 rounded-md border px-2 text-xs outline-none"
                    onChange={handleDeliveryChange}
                    value={deliveryMode}
                  >
                    <option value="auto">自动</option>
                    <option value="steer">建议</option>
                    <option value="queue">排队</option>
                    <option value="interrupt_and_send">打断</option>
                  </select>
                ) : null}
              </div>

              <Button
                disabled={!canSend}
                onClick={handleSend}
                size="sm"
                variant="primary"
              >
                <Icon icon={SentIcon} size="xs" />
                <span>发送</span>
              </Button>
            </div>
          </div>
        </FileInputDropzone>
      </FileInput>
    </div>
  );
}
