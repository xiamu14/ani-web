import {
  Cancel01Icon,
  DragDropVerticalIcon,
  LockIcon,
  LockOpenIcon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import type { DragEvent, MouseEvent } from "react";
import { useCallback, useState } from "react";
import { Badge, ROLE_META, STATUS_META } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import { writeDragRef } from "@/lib/drag-ref.ts";
import type { InteractionView } from "@/protocol/types.ts";
import type { RoleName, RoleTask } from "@/schemas/workspace.ts";
import { addTaskReference, setDraggingRef } from "@/store/composer.ts";
import {
  interactionStore,
  prioritizeInteraction,
} from "@/store/interactions.ts";
import { cancelRoleTask, workspaceStore } from "@/store/workspace.ts";

const INTERACTION_KIND_LABEL: Record<InteractionView["kind"], string> = {
  ask_user: "待选择",
  approval: "待授权",
  mcp_elicitation: "待确认",
};

function RoleTaskCard({
  isFocusedInteraction,
  pendingInteraction,
  task,
}: {
  readonly isFocusedInteraction: boolean;
  readonly pendingInteraction?: InteractionView;
  readonly task: RoleTask;
}) {
  const roleMeta = ROLE_META[task.role];
  const statusMeta = STATUS_META[task.status];
  const isExclusive = task.lockMode === "exclusive";
  const canStop = task.status === "running" || task.status === "queued";

  const handleCite = useCallback(() => {
    addTaskReference({
      taskId: task.id,
      taskCode: task.taskCode,
      role: task.role,
      title: task.title,
    });
  }, [task.id, task.role, task.taskCode, task.title]);

  const handleDragStart = useCallback(
    (event: DragEvent<HTMLButtonElement>) => {
      const payload = {
        kind: "task" as const,
        taskId: task.id,
        taskCode: task.taskCode,
        role: task.role,
        title: task.title,
      };
      writeDragRef(event.dataTransfer, payload);
      setDraggingRef({
        kind: "task",
        label: `@${task.role}(${task.taskCode})`,
        payload,
      });
    },
    [task.id, task.role, task.taskCode, task.title]
  );

  const handleDragEnd = useCallback(() => {
    setDraggingRef(null);
  }, []);

  const handleStop = useCallback(
    (event: MouseEvent<HTMLElement>) => {
      event.stopPropagation();
      void cancelRoleTask(task.id);
    },
    [task.id]
  );

  const handlePrioritize = useCallback(
    (event: MouseEvent<HTMLElement>) => {
      event.stopPropagation();
      if (pendingInteraction) {
        prioritizeInteraction(pendingInteraction.interaction_id);
      }
    },
    [pendingInteraction]
  );

  return (
    <li className="border-border-subtle bg-surface hover:border-accent/40 group flex flex-col gap-1.5 rounded-xl border p-2.5 shadow-xs transition-colors">
      <button
        className="flex w-full cursor-pointer flex-col gap-1.5 text-left"
        draggable
        onClick={handleCite}
        onDragEnd={handleDragEnd}
        onDragStart={handleDragStart}
        title="点击或拖拽到主对话中引用该任务"
        type="button"
      >
        <div className="flex w-full items-center justify-between gap-1.5">
          <div className="flex min-w-0 items-center gap-1.5">
            <Icon
              className="text-text-muted shrink-0 cursor-grab"
              icon={DragDropVerticalIcon}
              size="xs"
            />
            <span className="text-text-muted font-mono text-[11px] font-semibold">
              {task.taskCode}
            </span>
            <Badge tone={roleMeta.tone}>{roleMeta.label}</Badge>
          </div>
          {pendingInteraction ? (
            <Badge tone={isFocusedInteraction ? "accent" : "amber"}>
              {isFocusedInteraction
                ? "当前焦点"
                : INTERACTION_KIND_LABEL[pendingInteraction.kind]}
            </Badge>
          ) : null}
          <Badge tone={statusMeta.tone}>
            <span
              className={cn("size-1.5 rounded-full", statusMeta.dotClass)}
            />
            {statusMeta.label}
          </Badge>
        </div>

        <div className="text-text group-hover:text-accent w-full truncate text-[13px] font-medium transition-colors">
          {task.title}
        </div>
      </button>

      <div className="text-text-muted flex items-center justify-between gap-2 text-[11px]">
        <div className="flex items-center gap-1.5">
          <span
            className="inline-flex items-center gap-1"
            title={isExclusive ? "目录写锁" : "共享读锁"}
          >
            <Icon icon={isExclusive ? LockIcon : LockOpenIcon} size="xs" />
            <span>{isExclusive ? "写锁" : "读锁"}</span>
          </span>
          {task.activeToolName ? (
            <span className="bg-surface-subtle truncate rounded px-1 py-0.5 font-mono text-[10px]">
              {task.activeToolName}
            </span>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {pendingInteraction && !isFocusedInteraction ? (
            <Button onClick={handlePrioritize} size="xs" variant="subtle">
              优先答复
            </Button>
          ) : null}
          <Button onClick={handleCite} size="xs" variant="subtle">
            引用
          </Button>
          {canStop ? (
            <Button
              aria-label="取消任务"
              onClick={handleStop}
              size="xs"
              variant="ghost"
            >
              <Icon className="text-rose-500" icon={Cancel01Icon} size="xs" />
              <span>取消</span>
            </Button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

const ROLE_ORDER: readonly RoleName[] = [
  "coder",
  "reviewer",
  "tester",
  "researcher",
];

function RoleFilterChip({
  count,
  onToggle,
  role,
  selected,
}: {
  readonly count: number;
  readonly onToggle: (role: RoleName) => void;
  readonly role: RoleName;
  readonly selected: boolean;
}) {
  const meta = ROLE_META[role];
  const handleClick = useCallback(() => onToggle(role), [onToggle, role]);

  return (
    <button
      aria-pressed={selected}
      className={cn(
        "inline-flex cursor-pointer items-center gap-1 rounded-lg border px-2 py-1 text-[11px] transition-colors",
        selected
          ? "border-accent bg-accent/10 text-text font-medium"
          : "border-border-subtle bg-surface hover:bg-surface-hover text-text-secondary"
      )}
      onClick={handleClick}
      type="button"
    >
      <span>{meta.label}</span>
      <Badge tone={meta.tone}>{count}</Badge>
    </button>
  );
}

export function RoleTaskList() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const allTasks = useSelector(workspaceStore, (s) => s.tasks);
  const pendingInteractions = useSelector(interactionStore, (s) => s.pending);
  const [selectedRoles, setSelectedRoles] = useState<RoleName[]>([]);

  const routePending = pendingInteractions.filter(
    (item) => item.conversation_id === activeRouteId
  );
  const focusedInteractionId = routePending[0]?.interaction_id ?? null;
  const routeTasks = allTasks.filter((task) => task.routeId === activeRouteId);
  const counts: Record<RoleName, number> = {
    coder: 0,
    reviewer: 0,
    tester: 0,
    researcher: 0,
  };
  for (const task of routeTasks) {
    counts[task.role] += 1;
  }

  const handleToggleRole = useCallback((role: RoleName) => {
    setSelectedRoles((prev) =>
      prev.includes(role)
        ? prev.filter((item) => item !== role)
        : [...prev, role]
    );
  }, []);

  const handleClearRoles = useCallback(() => {
    setSelectedRoles([]);
  }, []);

  const tasks =
    selectedRoles.length > 0
      ? routeTasks.filter((task) => selectedRoles.includes(task.role))
      : routeTasks;

  return (
    <div className="flex flex-col gap-4">
      <section aria-label="角色筛选" className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between px-2">
          <span className="text-text-muted text-[11px] font-semibold tracking-wider uppercase">
            角色列表
          </span>
          {selectedRoles.length > 0 ? (
            <button
              className="text-accent cursor-pointer text-[11px] hover:underline"
              onClick={handleClearRoles}
              type="button"
            >
              全部
            </button>
          ) : null}
        </div>
        <div className="flex flex-wrap gap-1.5 px-1">
          {ROLE_ORDER.map((role) => (
            <RoleFilterChip
              count={counts[role]}
              key={role}
              onToggle={handleToggleRole}
              role={role}
              selected={selectedRoles.includes(role)}
            />
          ))}
        </div>
      </section>

      <section aria-label="角色任务" className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between px-2">
          <span className="text-text-muted text-[11px] font-semibold tracking-wider uppercase">
            角色任务
          </span>
          <span className="text-text-muted font-mono text-[11px]">
            {tasks.length}
          </span>
        </div>

        {tasks.length === 0 ? (
          <div className="border-border-subtle text-text-muted rounded-lg border border-dashed px-3 py-4 text-center text-xs">
            暂无任务
          </div>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {tasks.map((task) => {
              const pendingInteraction = routePending.find(
                (item) => item.task_id && `@${item.task_id}` === task.taskCode
              );
              return (
                <RoleTaskCard
                  isFocusedInteraction={Boolean(
                    pendingInteraction &&
                      pendingInteraction.interaction_id === focusedInteractionId
                  )}
                  key={task.id}
                  pendingInteraction={pendingInteraction}
                  task={task}
                />
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
