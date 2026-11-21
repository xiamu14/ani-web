import {
  Folder01Icon,
  HierarchyIcon,
  PieChartIcon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/react/tabs";
import { type } from "arktype";
import { motion } from "motion/react";
import { useCallback } from "react";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import { motionSpring } from "@/lib/motion.ts";
import type { DashboardTab } from "@/schemas/workspace.ts";
import { dashboardTabSchema } from "@/schemas/workspace.ts";
import { setDashboardTab, workspaceStore } from "@/store/workspace.ts";
import { ContextWindowTab } from "./context-window-tab.tsx";
import { ConversationTreeTab } from "./conversation-tree-tab.tsx";
import { WorkspaceFilesTab } from "./workspace-files-tab.tsx";

const TABS = [
  { value: "tree", label: "对话树", icon: HierarchyIcon },
  { value: "context", label: "上下文", icon: PieChartIcon },
  { value: "files", label: "工作区", icon: Folder01Icon },
] as const satisfies readonly {
  value: DashboardTab;
  label: string;
  icon: typeof HierarchyIcon;
}[];

export function InspectorPanel() {
  const dashboardTab = useSelector(workspaceStore, (s) => s.dashboardTab);

  const handleTabChange = useCallback((next: string | null) => {
    if (next) {
      const parsed = dashboardTabSchema(next);
      if (!(parsed instanceof type.errors)) {
        setDashboardTab(parsed);
      }
    }
  }, []);

  return (
    <Tabs
      className="flex h-full w-129 flex-col"
      onValueChange={handleTabChange}
      value={dashboardTab}
    >
      <header className="border-border-subtle flex h-12 shrink-0 items-center border-b px-3">
        <TabsList
          aria-label="运行仪表盘"
          className="bg-surface-subtle grid w-full grid-cols-3 gap-1 rounded-xl p-1"
        >
          {TABS.map((tab) => {
            const active = dashboardTab === tab.value;
            return (
              <TabsTab
                className={cn(
                  "focus-visible:ring-accent/40 relative flex cursor-pointer items-center justify-center gap-1.5 rounded-lg py-1.5 text-xs font-medium transition-colors outline-none focus-visible:ring-2",
                  active ? "text-text" : "text-text-secondary hover:text-text"
                )}
                key={tab.value}
                value={tab.value}
              >
                {active ? (
                  <motion.span
                    className="bg-surface border-border-subtle absolute inset-0 rounded-lg border shadow-xs"
                    layoutId="inspector-tab-pill"
                    transition={motionSpring.snappy}
                  />
                ) : null}
                <Icon className="relative z-10" icon={tab.icon} size="xs" />
                <span className="relative z-10">{tab.label}</span>
              </TabsTab>
            );
          })}
        </TabsList>
      </header>

      <TabsPanel
        className="flex min-h-0 flex-1 flex-col not-data-selected:hidden"
        keepMounted
        value="tree"
      >
        <ConversationTreeTab />
      </TabsPanel>

      <TabsPanel
        className="flex min-h-0 flex-1 flex-col not-data-selected:hidden"
        keepMounted
        value="context"
      >
        <ContextWindowTab />
      </TabsPanel>

      <TabsPanel
        className="flex min-h-0 flex-1 flex-col not-data-selected:hidden"
        keepMounted
        value="files"
      >
        <WorkspaceFilesTab />
      </TabsPanel>
    </Tabs>
  );
}
