import {
  AlertCircleIcon,
  ArrowRight01Icon,
  BotIcon,
  Clock01Icon,
  TerminalIcon,
  UserIcon,
  Wrench01Icon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import {
  Collapsible,
  CollapsiblePanel,
  CollapsibleTrigger,
} from "@ui-sdk/react/collapsible";
import { motion } from "motion/react";
import type { ReactNode } from "react";
import { useCallback } from "react";
import { Badge, ROLE_META, STATUS_META } from "@/components/ui/badge.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import { motionTransition } from "@/lib/motion.ts";
import type { ChatMessage, RoleTask, ToolTrace } from "@/schemas/workspace.ts";
import { selectTask, workspaceStore } from "@/store/workspace.ts";
import { ArtifactCard } from "./artifact-card.tsx";
import { MarkdownContent } from "./markdown-content.tsx";
import { ReferenceChip } from "./reference-chip.tsx";
import { SteeringSteps } from "./steering-steps.tsx";
import { ToolPayloadView } from "./tool-payload-view.tsx";

const TRACE_STATUS_DOT: Record<ToolTrace["status"], string> = {
  running: "animate-pulse bg-sky-500",
  error: "bg-rose-500",
  done: "bg-emerald-500",
};

function DispatchedTaskCard({ task }: { readonly task: RoleTask }) {
  const roleMeta = ROLE_META[task.role];
  const statusMeta = STATUS_META[task.status];
  const handleSelect = useCallback(() => selectTask(task.id), [task.id]);

  return (
    <button
      className="border-border-subtle bg-surface-subtle hover:border-accent/40 hover:bg-surface-hover flex cursor-pointer items-center justify-between gap-2 rounded-xl border px-3 py-2 text-left transition-colors"
      onClick={handleSelect}
      type="button"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="text-text-muted font-mono text-[11px] font-semibold">
            {task.taskCode}
          </span>
          <Badge tone={roleMeta.tone}>{roleMeta.label}</Badge>
        </div>
        <div className="text-text mt-1 truncate text-xs font-medium">
          {task.title}
        </div>
      </div>
      <Badge tone={statusMeta.tone}>{statusMeta.label}</Badge>
    </button>
  );
}

function DispatchedTasks({ taskIds }: { readonly taskIds: string[] }) {
  const allTasks = useSelector(workspaceStore, (s) => s.tasks);
  const matched = allTasks.filter((task) => taskIds.includes(task.id));
  if (matched.length === 0) {
    return null;
  }

  return (
    <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
      {matched.map((task) => (
        <DispatchedTaskCard key={task.id} task={task} />
      ))}
    </div>
  );
}

function MessageArtifacts({ artifactIds }: { readonly artifactIds: string[] }) {
  const artifacts = useSelector(workspaceStore, (s) => s.artifacts);
  const matched = artifacts.filter((item) => artifactIds.includes(item.id));
  if (matched.length === 0) {
    return null;
  }

  return (
    <div className="mt-3 flex flex-col gap-2">
      {matched.map((artifact) => (
        <ArtifactCard artifact={artifact} key={artifact.id} />
      ))}
    </div>
  );
}

function MessageThinkingBlock({ thinking }: { readonly thinking: string }) {
  return (
    <Collapsible className="mb-2">
      <CollapsibleTrigger className="group text-text-muted hover:text-text flex cursor-pointer items-center gap-1 text-xs font-medium">
        <Icon
          className="transition-transform group-data-open:rotate-90"
          icon={ArrowRight01Icon}
          size="xs"
        />
        <span>思考过程</span>
      </CollapsibleTrigger>
      <CollapsiblePanel className="text-text-muted mt-1.5 text-xs leading-relaxed whitespace-pre-wrap">
        {thinking}
      </CollapsiblePanel>
    </Collapsible>
  );
}

function MessageCitations({
  citations,
}: {
  readonly citations?: ChatMessage["citations"];
}) {
  if (!citations || citations.length === 0) {
    return null;
  }
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {citations.map((citation) => (
        <span
          className="border-border-subtle bg-surface-subtle text-text-secondary inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px]"
          key={citation.id}
          title={citation.summary}
        >
          <span className="font-mono">{citation.sourceType}</span>
          <span>{citation.label}</span>
        </span>
      ))}
    </div>
  );
}

function MessageSuggestedActions({
  actions,
}: {
  readonly actions?: ChatMessage["suggestedActions"];
}) {
  if (!actions || actions.length === 0) {
    return null;
  }
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {actions.map((act) => (
        <span
          className="border-accent/30 bg-accent/10 text-accent inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-medium"
          key={act.id}
        >
          {act.label}
        </span>
      ))}
    </div>
  );
}

function MessageToolTraces({
  hasContentAbove,
  traces,
}: {
  readonly hasContentAbove: boolean;
  readonly traces: NonNullable<ChatMessage["toolTraces"]>;
}) {
  return (
    <Collapsible
      className={cn(
        hasContentAbove ? "border-border-subtle mt-2.5 border-t pt-2" : ""
      )}
    >
      <CollapsibleTrigger className="group text-text-secondary hover:text-text flex cursor-pointer items-center gap-1.5 text-xs font-medium">
        <Icon
          className="transition-transform group-data-open:rotate-90"
          icon={ArrowRight01Icon}
          size="xs"
        />
        <Icon icon={Wrench01Icon} size="xs" />
        <span>工具调用 ({traces.length})</span>
      </CollapsibleTrigger>
      <CollapsiblePanel className="mt-2 flex flex-col gap-1.5">
        {traces.map((trace) => (
          <div
            className="bg-surface-subtle rounded-lg px-2.5 py-2 font-mono text-[11px]"
            key={trace.id}
          >
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-1.5">
                <span
                  className={cn(
                    "size-1.5 shrink-0 rounded-full",
                    TRACE_STATUS_DOT[trace.status]
                  )}
                />
                <span className="text-text font-medium">{trace.name}</span>
              </div>
              <span className="text-text-muted inline-flex shrink-0 items-center gap-1">
                <Icon icon={Clock01Icon} size="xs" />
                {trace.durationMs}ms
              </span>
            </div>
            <ToolPayloadView content={trace.argsSummary} />
          </div>
        ))}
      </CollapsiblePanel>
    </Collapsible>
  );
}

function MessageReferences({
  references,
  isUser,
}: {
  readonly references: NonNullable<ChatMessage["references"]>;
  readonly isUser: boolean;
}) {
  return (
    <div className="mb-2 flex flex-wrap gap-1.5">
      {references.map((reference) => (
        <ReferenceChip
          key={reference.id}
          reference={reference}
          tone={isUser ? "inverted" : "surface"}
        />
      ))}
    </div>
  );
}

function MessageBody({
  content,
  error,
  isUser,
  streaming,
}: {
  readonly content: string;
  readonly error?: string;
  readonly isUser: boolean;
  readonly streaming?: boolean;
}) {
  let contentNode: ReactNode = null;
  if (content) {
    contentNode = isUser ? (
      <p className="whitespace-pre-wrap">{content}</p>
    ) : (
      <MarkdownContent content={content} />
    );
  }

  return (
    <>
      {contentNode}

      {streaming && !content ? (
        <div className="text-text-muted flex items-center gap-1.5 text-xs">
          <span className="bg-accent inline-block size-2 animate-pulse rounded-full" />
          <span>处理中</span>
        </div>
      ) : null}

      {error ? (
        <div className="mt-2 flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-2.5 py-2 text-xs text-rose-500">
          <Icon className="mt-0.5 shrink-0" icon={AlertCircleIcon} size="xs" />
          <span>{error}</span>
        </div>
      ) : null}
    </>
  );
}

function SystemMessageItem({ message }: { readonly message: ChatMessage }) {
  const isError = Boolean(message.error);
  return (
    <motion.article
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "flex items-center gap-2.5 rounded-xl border px-3.5 py-2.5 text-xs",
        isError
          ? "border-rose-500/30 bg-rose-500/10 text-rose-500"
          : "border-border-subtle bg-surface-subtle/70 text-text-secondary"
      )}
      initial={{ opacity: 0, y: 6 }}
      transition={motionTransition.fast}
    >
      <Icon
        className={isError ? "shrink-0 text-rose-500" : "text-accent shrink-0"}
        icon={isError ? AlertCircleIcon : TerminalIcon}
        size="xs"
      />
      <span className="font-medium">{message.senderLabel}</span>
      <span className="min-w-0 flex-1">{message.error ?? message.content}</span>
      <span className="text-text-muted shrink-0 font-mono text-[11px]">
        {message.createdAt}
      </span>
    </motion.article>
  );
}

function MessageContentArea({
  isUser,
  message,
}: {
  readonly isUser: boolean;
  readonly message: ChatMessage;
}) {
  if (message.steps && message.steps.length > 0) {
    return (
      <>
        <SteeringSteps steps={message.steps} streaming={message.streaming} />
        {message.content ? (
          <div className="border-border-subtle mt-1 border-t pt-2.5">
            <MessageBody
              content={message.content}
              error={message.error}
              isUser={isUser}
              streaming={message.streaming}
            />
          </div>
        ) : (
          <MessageBody
            content=""
            error={message.error}
            isUser={isUser}
            streaming={false}
          />
        )}
      </>
    );
  }

  return (
    <>
      {message.thinking ? (
        <MessageThinkingBlock thinking={message.thinking} />
      ) : null}

      <MessageBody
        content={message.content}
        error={message.error}
        isUser={isUser}
        streaming={message.streaming}
      />

      {message.toolTraces && message.toolTraces.length > 0 ? (
        <MessageToolTraces
          hasContentAbove={Boolean(
            message.content || message.references?.length
          )}
          traces={message.toolTraces}
        />
      ) : null}
    </>
  );
}

export function MessageItem({ message }: { readonly message: ChatMessage }) {
  if (message.sender === "system") {
    return <SystemMessageItem message={message} />;
  }
  const isUser = message.sender === "user";

  return (
    <motion.article
      animate={{ opacity: 1, y: 0 }}
      className={cn("flex gap-3", isUser ? "flex-row-reverse" : "flex-row")}
      initial={{ opacity: 0, y: 6 }}
      transition={motionTransition.fast}
    >
      <div
        className={cn(
          "flex size-7 shrink-0 items-center justify-center rounded-lg border",
          isUser
            ? "border-accent/25 bg-accent text-white"
            : "border-border-subtle bg-surface text-accent"
        )}
      >
        <Icon icon={isUser ? UserIcon : BotIcon} size="xs" />
      </div>

      <div
        className={cn(
          "flex max-w-[85%] min-w-0 flex-col",
          isUser ? "items-end" : "items-start"
        )}
      >
        <div className="mb-1 flex items-center gap-2 text-xs">
          <span className="text-text font-medium">{message.senderLabel}</span>
          {message.role ? (
            <Badge tone={ROLE_META[message.role].tone}>
              {ROLE_META[message.role].label}
            </Badge>
          ) : null}
          {message.streaming ? <Badge tone="sky">生成中</Badge> : null}
          <span className="text-text-muted font-mono text-[11px]">
            {message.createdAt}
          </span>
        </div>

        <div
          className={cn(
            "rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
            isUser
              ? "bg-accent rounded-tr-sm text-white"
              : "bg-surface border-border-subtle text-text rounded-tl-sm border shadow-xs"
          )}
        >
          {message.references && message.references.length > 0 ? (
            <MessageReferences
              isUser={isUser}
              references={message.references}
            />
          ) : null}

          <MessageContentArea isUser={isUser} message={message} />

          <MessageCitations citations={message.citations} />

          <MessageSuggestedActions actions={message.suggestedActions} />

          {message.dispatchedTaskIds && message.dispatchedTaskIds.length > 0 ? (
            <DispatchedTasks taskIds={message.dispatchedTaskIds} />
          ) : null}

          {message.artifactIds && message.artifactIds.length > 0 ? (
            <MessageArtifacts artifactIds={message.artifactIds} />
          ) : null}
        </div>
      </div>
    </motion.article>
  );
}
