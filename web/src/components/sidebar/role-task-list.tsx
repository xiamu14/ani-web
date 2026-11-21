import {
  Cancel01Icon,
  DragDropVerticalIcon,
  LockIcon,
  LockOpenIcon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import type { DragEvent, MouseEvent } from "react";
import { useCallback } from "react";
import { Badge, ROLE_META, STATUS_META } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import { writeDragRef } from "@/lib/drag-ref.ts";
import type { RoleTask } from "@/schemas/workspace.ts";
import { addTaskReference, setDraggingRef } from "@/store/composer.ts";
import { cancelRoleTask, workspaceStore } from "@/store/workspace.ts";

function RoleTaskCard({ task }: { readonly task: RoleTask }) {
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

export function RoleTaskList() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const allTasks = useSelector(workspaceStore, (s) => s.tasks);

  const tasks = allTasks.filter((task) => task.routeId === activeRouteId);

  return (
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
          {tasks.map((task) => (
            <RoleTaskCard key={task.id} task={task} />
          ))}
        </ul>
      )}
    </section>
  );
}
