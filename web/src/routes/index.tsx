import { createFileRoute } from "@tanstack/react-router";
import { WorkbenchShell } from "@/components/layout/workbench-shell.tsx";

export const Route = createFileRoute("/")({
  head: () => ({ meta: [{ title: "Agent 工作台" }] }),
  component: WorkbenchShell,
});
