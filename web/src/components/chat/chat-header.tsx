import {
  ArrowLeft01Icon,
  Cancel01Icon,
  Folder01Icon,
  PanelLeftIcon,
  PanelRightIcon,
  PieChartIcon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import { useCallback } from "react";
import { Badge, ROLE_META, STATUS_META } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { TooltipHint } from "@/components/ui/tooltip.tsx";
import { formatPercent, pathBasename } from "@/lib/format.ts";
import { connectionStore } from "@/store/connection.ts";
import {
  interruptActiveRun,
  returnToRoute,
  setDashboardTab,
  toggleLeftPanel,
  toggleRightPanel,
  workspaceStore,
} from "@/store/workspace.ts";

function openFilesTab() {
  setDashboardTab("files");
}

function openContextTab() {
  setDashboardTab("context");
}

export function ChatHeader() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const activeTaskId = useSelector(workspaceStore, (s) => s.activeTaskId);
  const routes = useSelector(workspaceStore, (s) => s.routes);
  const tasks = useSelector(workspaceStore, (s) => s.tasks);
  const contextByThread = useSelector(workspaceStore, (s) => s.contextByThread);
  const leftOpen = useSelector(workspaceStore, (s) => s.leftPanelOpen);
  const rightOpen = useSelector(workspaceStore, (s) => s.rightPanelOpen);
  const runningConversationIds = useSelector(
    connectionStore,
    (s) => s.runningConversationIds
  );

  const isRunning = runningConversationIds.includes(activeRouteId);
  const handleInterrupt = useCallback(() => {
    void interruptActiveRun();
  }, []);

  const route = routes.find((item) => item.id === activeRouteId);
  const task = activeTaskId
    ? tasks.find((item) => item.id === activeTaskId)
    : undefined;
  const threadId = activeTaskId ?? activeRouteId;
  const context = contextByThread[threadId];
  const percent = context
    ? formatPercent(context.usedTokens, context.maxTokens)
    : 0;

  return (
    <header className="border-border-subtle bg-stage flex h-12 shrink-0 items-center justify-between gap-3 border-b px-4">
      <div className="flex min-w-0 items-center gap-2">
        <TooltipHint
          label={leftOpen ? "收起左侧栏" : "展开左侧栏"}
          side="bottom"
        >
          <Button
            aria-label="切换左侧栏"
            onClick={toggleLeftPanel}
            size="icon"
            variant="ghost"
          >
            <Icon icon={PanelLeftIcon} size="sm" />
          </Button>
        </TooltipHint>

        {task ? (
          <Button onClick={returnToRoute} size="xs" variant="subtle">
            <Icon icon={ArrowLeft01Icon} size="xs" />
            <span>返回路由</span>
          </Button>
        ) : null}

        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate text-sm font-semibold">
            {task
              ? `${task.taskCode} ${task.title}`
              : (route?.title ?? "未连接会话")}
          </h1>
          {isRunning ? (
            <Badge tone="sky">
              <span className="size-1.5 animate-pulse rounded-full bg-sky-500" />
              运行中
            </Badge>
          ) : null}
          {task ? (
            <>
              <Badge tone={ROLE_META[task.role].tone}>
                {ROLE_META[task.role].label}
              </Badge>
              <Badge tone={STATUS_META[task.status].tone}>
                {STATUS_META[task.status].label}
              </Badge>
            </>
          ) : null}
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {isRunning ? (
          <Button onClick={handleInterrupt} size="xs" variant="subtle">
            <Icon className="text-destructive" icon={Cancel01Icon} size="xs" />
            <span>中断</span>
          </Button>
        ) : null}

        {route ? (
          <button
            className="border-border-subtle bg-surface-subtle text-text-secondary hover:bg-surface-hover hidden cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 py-1 font-mono text-xs transition-colors sm:inline-flex"
            onClick={openFilesTab}
            type="button"
          >
            <Icon icon={Folder01Icon} size="xs" />
            <span>{pathBasename(task?.workPath ?? route.workPath)}</span>
          </button>
        ) : null}

        <button
          className="border-border-subtle bg-surface-subtle text-text-secondary hover:bg-surface-hover inline-flex cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium tabular-nums transition-colors"
          onClick={openContextTab}
          type="button"
        >
          <Icon className="text-accent" icon={PieChartIcon} size="xs" />
          <span>上下文 {percent}%</span>
        </button>

        <TooltipHint
          label={rightOpen ? "收起仪表盘" : "展开仪表盘"}
          side="bottom"
        >
          <Button
            aria-label="切换右侧仪表盘"
            onClick={toggleRightPanel}
            size="icon"
            variant="ghost"
          >
            <Icon icon={PanelRightIcon} size="sm" />
          </Button>
        </TooltipHint>
      </div>
    </header>
  );
}
