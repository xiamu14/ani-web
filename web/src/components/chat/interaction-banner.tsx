import {
  AlertCircleIcon,
  CheckIcon,
  LockIcon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import type { ChangeEvent } from "react";
import { useCallback, useState } from "react";
import { Badge } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import type {
  ApprovalItemView,
  ApprovalPayloadView,
  AskUserPayloadView,
  AskUserQuestionView,
  InteractionView,
  McpElicitationPayloadView,
} from "@/protocol/types.ts";
import {
  interactionStore,
  submitInteractionResponse,
} from "@/store/interactions.ts";

function ApprovalItemRow({
  item,
  checked,
  onToggle,
}: {
  readonly item: ApprovalItemView;
  readonly checked: boolean;
  readonly onToggle: (itemId: string) => void;
}) {
  const handleChange = useCallback(() => {
    onToggle(item.item_id);
  }, [item.item_id, onToggle]);

  return (
    <label className="border-border-subtle bg-surface flex cursor-pointer items-start gap-2 rounded-lg border px-2.5 py-2 text-xs">
      <input
        checked={checked}
        className="mt-0.5"
        onChange={handleChange}
        type="checkbox"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 font-medium">
          <span>{item.tool_name}</span>
          <span className="text-muted-foreground text-[11px]">
            {item.category}
          </span>
        </div>
        <p className="text-muted-foreground mt-0.5 truncate text-[11px]">
          {item.reason}
        </p>
      </div>
    </label>
  );
}

function CategoryGrantCheckbox({
  category,
  checked,
  onToggle,
}: {
  readonly category: string;
  readonly checked: boolean;
  readonly onToggle: (category: string) => void;
}) {
  const handleChange = useCallback(() => {
    onToggle(category);
  }, [category, onToggle]);

  return (
    <label className="text-muted-foreground flex cursor-pointer items-center gap-1 text-[11px]">
      <input checked={checked} onChange={handleChange} type="checkbox" />
      <span>本次放行 {category}</span>
    </label>
  );
}

function ApprovalCard({
  interaction,
  payload,
  submitting,
  error,
}: {
  readonly interaction: InteractionView;
  readonly payload: ApprovalPayloadView;
  readonly submitting: boolean;
  readonly error: string | null;
}) {
  const [decisions, setDecisions] = useState<Record<string, boolean>>(() => {
    const initial: Record<string, boolean> = {};
    for (const item of payload.items) {
      initial[item.item_id] = true;
    }
    return initial;
  });
  const [grantCategories, setGrantCategories] = useState<string[]>([]);

  const handleToggleItem = useCallback((itemId: string) => {
    setDecisions((prev) => ({ ...prev, [itemId]: !prev[itemId] }));
  }, []);

  const handleToggleCategory = useCallback((category: string) => {
    setGrantCategories((prev) =>
      prev.includes(category)
        ? prev.filter((item) => item !== category)
        : [...prev, category]
    );
  }, []);

  const handleApproveSelected = useCallback(() => {
    const itemDecisions: Record<string, { approved: boolean }> = {};
    for (const item of payload.items) {
      itemDecisions[item.item_id] = {
        approved: decisions[item.item_id] ?? true,
      };
    }
    void submitInteractionResponse(interaction.interaction_id, {
      kind: "approval",
      item_decisions: itemDecisions,
      ...(grantCategories.length > 0
        ? { grant_categories_for_run: grantCategories }
        : {}),
    });
  }, [decisions, grantCategories, interaction.interaction_id, payload.items]);

  const handleRejectAll = useCallback(() => {
    void submitInteractionResponse(interaction.interaction_id, {
      kind: "approval",
      approved: false,
      reason: "用户拒绝执行",
    });
  }, [interaction.interaction_id]);

  const categories = Array.from(
    new Set(payload.items.map((item) => item.category))
  );

  return (
    <div className="bg-surface-elevated rounded-xl border border-amber-500/30 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold">
          <Icon className="text-amber-500" icon={LockIcon} size="xs" />
          <span>权限确认</span>
          {interaction.role_name ? (
            <Badge tone="neutral">@{interaction.role_name}</Badge>
          ) : null}
        </div>
        <span className="text-muted-foreground text-[11px]">
          {payload.items.length} 项待确认
        </span>
      </div>

      <div className="flex flex-col gap-1.5">
        {payload.items.map((item) => (
          <ApprovalItemRow
            checked={decisions[item.item_id] ?? true}
            item={item}
            key={item.item_id}
            onToggle={handleToggleItem}
          />
        ))}
      </div>

      {categories.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {categories.map((cat) => (
            <CategoryGrantCheckbox
              category={cat}
              checked={grantCategories.includes(cat)}
              key={cat}
              onToggle={handleToggleCategory}
            />
          ))}
        </div>
      ) : null}

      {error ? (
        <p className="text-destructive mt-2 text-[11px]">{error}</p>
      ) : null}

      <div className="mt-2.5 flex items-center justify-end gap-1.5">
        <Button
          disabled={submitting}
          onClick={handleRejectAll}
          size="sm"
          variant="ghost"
        >
          拒绝
        </Button>
        <Button
          disabled={submitting}
          onClick={handleApproveSelected}
          size="sm"
          variant="primary"
        >
          <Icon icon={CheckIcon} size="xs" />
          <span>确认执行</span>
        </Button>
      </div>
    </div>
  );
}

function AskUserOptionButton({
  header,
  label,
  multi,
  active,
  onToggle,
}: {
  readonly header: string;
  readonly label: string;
  readonly multi: boolean;
  readonly active: boolean;
  readonly onToggle: (header: string, label: string, multi: boolean) => void;
}) {
  const handleClick = useCallback(() => {
    onToggle(header, label, multi);
  }, [header, label, multi, onToggle]);

  return (
    <button
      className={cn(
        "rounded-lg border px-2.5 py-1 text-xs transition-colors",
        active
          ? "border-accent bg-accent/15 text-accent font-medium"
          : "border-border-subtle bg-surface hover:bg-surface-hover"
      )}
      onClick={handleClick}
      type="button"
    >
      {label}
    </button>
  );
}

function AskUserQuestionBlock({
  question,
  selected,
  customValue,
  onToggleOption,
  onCustomChange,
}: {
  readonly question: AskUserQuestionView;
  readonly selected: readonly string[];
  readonly customValue: string;
  readonly onToggleOption: (
    header: string,
    label: string,
    multi: boolean
  ) => void;
  readonly onCustomChange: (header: string, value: string) => void;
}) {
  const handleInput = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const { value } = event.target;
      onCustomChange(question.header, value);
    },
    [onCustomChange, question.header]
  );

  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-xs font-medium">{question.question}</div>
      <div className="flex flex-wrap gap-1.5">
        {question.options.map((opt) => (
          <AskUserOptionButton
            active={selected.includes(opt.label)}
            header={question.header}
            key={opt.label}
            label={opt.label}
            multi={question.multi_select}
            onToggle={onToggleOption}
          />
        ))}
      </div>
      <input
        className="border-border-subtle bg-surface focus:border-accent h-7 rounded-lg border px-2 text-xs outline-none"
        onChange={handleInput}
        placeholder="补充说明"
        type="text"
        value={customValue}
      />
    </div>
  );
}

function AskUserCard({
  interaction,
  payload,
  submitting,
  error,
}: {
  readonly interaction: InteractionView;
  readonly payload: AskUserPayloadView;
  readonly submitting: boolean;
  readonly error: string | null;
}) {
  const [selectedByHeader, setSelectedByHeader] = useState<
    Record<string, string[]>
  >({});
  const [customByHeader, setCustomByHeader] = useState<Record<string, string>>(
    {}
  );

  const toggleOption = useCallback(
    (header: string, label: string, multi: boolean) => {
      setSelectedByHeader((prev) => {
        const current = prev[header] ?? [];
        if (multi) {
          const next = current.includes(label)
            ? current.filter((item) => item !== label)
            : [...current, label];
          return { ...prev, [header]: next };
        }
        return { ...prev, [header]: current.includes(label) ? [] : [label] };
      });
    },
    []
  );

  const handleCustomChange = useCallback((header: string, value: string) => {
    setCustomByHeader((prev) => ({ ...prev, [header]: value }));
  }, []);

  const handleSubmit = useCallback(() => {
    const answers = payload.questions.map((question) => {
      const customText = customByHeader[question.header]?.trim();
      return {
        header: question.header,
        selected: selectedByHeader[question.header] ?? [],
        ...(customText ? { custom_answer: customText } : {}),
      };
    });
    void submitInteractionResponse(interaction.interaction_id, {
      kind: "ask_user",
      answers,
    });
  }, [
    customByHeader,
    interaction.interaction_id,
    payload.questions,
    selectedByHeader,
  ]);

  const handleCancel = useCallback(() => {
    void submitInteractionResponse(interaction.interaction_id, {
      kind: "ask_user",
      cancelled: true,
      answers: [],
    });
  }, [interaction.interaction_id]);

  return (
    <div className="border-accent/30 bg-surface-elevated rounded-xl border p-3">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold">
        <Icon className="text-accent" icon={AlertCircleIcon} size="xs" />
        <span>需要确认</span>
      </div>

      <div className="flex flex-col gap-3">
        {payload.questions.map((question) => (
          <AskUserQuestionBlock
            customValue={customByHeader[question.header] ?? ""}
            key={question.header}
            onCustomChange={handleCustomChange}
            onToggleOption={toggleOption}
            question={question}
            selected={selectedByHeader[question.header] ?? []}
          />
        ))}
      </div>

      {error ? (
        <p className="text-destructive mt-2 text-[11px]">{error}</p>
      ) : null}

      <div className="mt-2.5 flex items-center justify-end gap-1.5">
        <Button
          disabled={submitting}
          onClick={handleCancel}
          size="sm"
          variant="ghost"
        >
          跳过
        </Button>
        <Button
          disabled={submitting}
          onClick={handleSubmit}
          size="sm"
          variant="primary"
        >
          提交
        </Button>
      </div>
    </div>
  );
}

function ElicitationCard({
  interaction,
  payload,
  submitting,
  error,
}: {
  readonly interaction: InteractionView;
  readonly payload: McpElicitationPayloadView;
  readonly submitting: boolean;
  readonly error: string | null;
}) {
  const handleDecline = useCallback(() => {
    void submitInteractionResponse(interaction.interaction_id, {
      kind: "mcp_elicitation",
      action: "decline",
    });
  }, [interaction.interaction_id]);

  const handleAccept = useCallback(() => {
    void submitInteractionResponse(interaction.interaction_id, {
      kind: "mcp_elicitation",
      action: "accept",
    });
  }, [interaction.interaction_id]);

  return (
    <div className="border-border bg-surface-elevated rounded-xl border p-3">
      <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold">
        <span>扩展请求</span>
        <Badge tone="neutral">{payload.server_name}</Badge>
      </div>
      <p className="text-muted-foreground text-xs">{payload.message}</p>
      {error ? (
        <p className="text-destructive mt-2 text-[11px]">{error}</p>
      ) : null}
      <div className="mt-2.5 flex items-center justify-end gap-1.5">
        <Button
          disabled={submitting}
          onClick={handleDecline}
          size="sm"
          variant="ghost"
        >
          拒绝
        </Button>
        <Button
          disabled={submitting}
          onClick={handleAccept}
          size="sm"
          variant="primary"
        >
          允许
        </Button>
      </div>
    </div>
  );
}

export function InteractionBanner() {
  const pending = useSelector(interactionStore, (s) => s.pending);
  const submittingIds = useSelector(interactionStore, (s) => s.submittingIds);
  const errorById = useSelector(interactionStore, (s) => s.errorById);

  if (pending.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-col gap-3">
      {pending.map((item) => {
        const submitting = submittingIds[item.interaction_id] ?? false;
        const error = errorById[item.interaction_id] ?? null;

        if (item.kind === "approval" && item.approval) {
          return (
            <ApprovalCard
              error={error}
              interaction={item}
              key={item.interaction_id}
              payload={item.approval}
              submitting={submitting}
            />
          );
        }
        if (item.kind === "ask_user" && item.ask_user) {
          return (
            <AskUserCard
              error={error}
              interaction={item}
              key={item.interaction_id}
              payload={item.ask_user}
              submitting={submitting}
            />
          );
        }
        if (item.kind === "mcp_elicitation" && item.mcp_elicitation) {
          return (
            <ElicitationCard
              error={error}
              interaction={item}
              key={item.interaction_id}
              payload={item.mcp_elicitation}
              submitting={submitting}
            />
          );
        }
        return null;
      })}
    </div>
  );
}
