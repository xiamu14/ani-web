import { useSelector } from "@tanstack/react-store";
import { AnimatePresence, motion } from "motion/react";
import { useEffect } from "react";
import { ChatPane } from "@/components/chat/chat-pane.tsx";
import { InspectorPanel } from "@/components/inspector/inspector-panel.tsx";
import { LeftSidebar } from "@/components/sidebar/left-sidebar.tsx";
import { motionTransition } from "@/lib/motion.ts";
import { discoverAndConnectRuntime } from "@/store/connection.ts";
import { workspaceStore } from "@/store/workspace.ts";

export function WorkbenchShell() {
  const leftOpen = useSelector(workspaceStore, (s) => s.leftPanelOpen);
  const rightOpen = useSelector(workspaceStore, (s) => s.rightPanelOpen);

  useEffect(() => {
    void discoverAndConnectRuntime();
  }, []);

  return (
    <div className="bg-bg text-text flex h-dvh w-screen overflow-hidden">
      <AnimatePresence initial={false}>
        {leftOpen && (
          <motion.aside
            animate={{ width: 268, opacity: 1 }}
            className="border-border bg-rail shrink-0 overflow-hidden border-r"
            exit={{ width: 0, opacity: 0 }}
            initial={{ width: 0, opacity: 0 }}
            transition={motionTransition.base}
          >
            <LeftSidebar />
          </motion.aside>
        )}
      </AnimatePresence>

      <main className="bg-stage flex min-w-0 flex-1 flex-col">
        <ChatPane />
      </main>

      <AnimatePresence initial={false}>
        {rightOpen && (
          <motion.aside
            animate={{ width: 516, opacity: 1 }}
            className="border-border bg-rail shrink-0 overflow-hidden border-l"
            exit={{ width: 0, opacity: 0 }}
            initial={{ width: 0, opacity: 0 }}
            transition={motionTransition.base}
          >
            <InspectorPanel />
          </motion.aside>
        )}
      </AnimatePresence>
    </div>
  );
}
