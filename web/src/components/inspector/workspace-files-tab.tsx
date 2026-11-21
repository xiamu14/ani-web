import {
  ArrowRight01Icon,
  DragDropVerticalIcon,
  File01Icon,
  FileCodeIcon,
  FileTextIcon,
  Folder01Icon,
  FolderOpenIcon,
  Link01Icon,
  LockIcon,
  LockOpenIcon,
  Refresh01Icon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import {
  ScrollArea,
  ScrollAreaContent,
  ScrollAreaScrollbar,
  ScrollAreaThumb,
  ScrollAreaViewport,
} from "@ui-sdk/react/scroll-area";
import {
  Tree,
  TreeGroup,
  TreeItem,
  TreeItemIndicator,
  TreeItemLabel,
} from "@ui-sdk/react/tree";
import type { DragEvent } from "react";
import { useCallback, useEffect } from "react";
import { Badge } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import type { DragRefPayload } from "@/lib/drag-ref.ts";
import { writeDragRef } from "@/lib/drag-ref.ts";
import { pathBasename } from "@/lib/format.ts";
import type { WorkspaceFileNode } from "@/schemas/workspace.ts";
import { addWorkspaceFileReference, setDraggingRef } from "@/store/composer.ts";
import {
  loadWorkspaceFiles,
  selectFile,
  workspaceStore,
} from "@/store/workspace.ts";
import { ArtifactList } from "./artifact-list.tsx";

const CHANGE_BADGE: Record<
  NonNullable<WorkspaceFileNode["change"]>,
  { label: string; text: string; tone: "amber" | "emerald" | "sky" }
> = {
  modified: { label: "M", text: "已修改", tone: "amber" },
  added: { label: "A", text: "已新增", tone: "emerald" },
  read: { label: "R", text: "已读取", tone: "sky" },
};

function resolveFileIcon(name: string) {
  if (name.endsWith(".md") || name.endsWith(".txt")) {
    return FileTextIcon;
  }
  if (
    name.endsWith(".ts") ||
    name.endsWith(".tsx") ||
    name.endsWith(".py") ||
    name.endsWith(".json") ||
    name.endsWith(".jsonc") ||
    name.endsWith(".css")
  ) {
    return FileCodeIcon;
  }
  return File01Icon;
}

function findFileNode(
  nodes: readonly WorkspaceFileNode[],
  targetPath: string | null
): WorkspaceFileNode | null {
  if (!targetPath) {
    return null;
  }
  for (const node of nodes) {
    if (node.path === targetPath) {
      return node;
    }
    if (node.children) {
      const hit = findFileNode(node.children, targetPath);
      if (hit) {
        return hit;
      }
    }
  }
  return null;
}

function WorkspaceFileRow({ node }: { readonly node: WorkspaceFileNode }) {
  const isDir = node.kind === "dir";
  const changeMeta = node.change ? CHANGE_BADGE[node.change] : null;

  const handleDragStart = useCallback(
    (event: DragEvent<HTMLSpanElement>) => {
      event.stopPropagation();
      const payload: DragRefPayload = {
        kind: "file",
        path: node.path,
        name: node.name,
        isDir,
        ...(node.sizeLabel ? { sizeLabel: node.sizeLabel } : {}),
      };
      writeDragRef(event.dataTransfer, payload);
      setDraggingRef({ kind: "file", label: node.path, payload });
    },
    [isDir, node.name, node.path, node.sizeLabel]
  );

  const handleDragEnd = useCallback(() => {
    setDraggingRef(null);
  }, []);

  return (
    <TreeItem key={node.path} value={node.path}>
      <TreeItemLabel
        className={cn(
          "group flex cursor-grab items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-xs transition-colors active:cursor-grabbing",
          "hover:bg-surface-hover data-selected:bg-surface data-selected:shadow-xs"
        )}
        draggable
        onDragEnd={handleDragEnd}
        onDragStart={handleDragStart}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          {isDir ? (
            <>
              <TreeItemIndicator className="text-text-muted inline-flex size-4 shrink-0 items-center justify-center transition-transform data-expanded:rotate-90">
                <Icon icon={ArrowRight01Icon} size="xs" />
              </TreeItemIndicator>
              <Icon
                className="text-amber-500 group-data-expanded:hidden"
                icon={Folder01Icon}
                size="xs"
              />
              <Icon
                className="hidden text-amber-500 group-data-expanded:inline-block"
                icon={FolderOpenIcon}
                size="xs"
              />
            </>
          ) : (
            <>
              <span className="size-4 shrink-0" />
              <Icon
                className="text-text-secondary"
                icon={resolveFileIcon(node.name)}
                size="xs"
              />
            </>
          )}
          <span className="text-text truncate font-mono text-[12px]">
            {node.name}
          </span>
        </span>

        <span className="flex shrink-0 items-center gap-1">
          <Icon
            className="text-text-muted opacity-0 transition-opacity group-hover:opacity-100"
            icon={DragDropVerticalIcon}
            size="xs"
          />
          {changeMeta ? (
            <Badge className="font-mono" tone={changeMeta.tone}>
              {changeMeta.label}
            </Badge>
          ) : null}
        </span>
      </TreeItemLabel>

      {node.children && node.children.length > 0 ? (
        <TreeGroup className="border-border-subtle ml-3.5 flex flex-col gap-0.5 border-l pl-2">
          <WorkspaceFileRows nodes={node.children} />
        </TreeGroup>
      ) : null}
    </TreeItem>
  );
}

function WorkspaceFileRows({
  nodes,
}: {
  readonly nodes: readonly WorkspaceFileNode[];
}) {
  return nodes.map((node) => <WorkspaceFileRow key={node.path} node={node} />);
}

export function WorkspaceFilesTab() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const routes = useSelector(workspaceStore, (s) => s.routes);
  const tasks = useSelector(workspaceStore, (s) => s.tasks);
  const filesByRoute = useSelector(workspaceStore, (s) => s.filesByRoute);
  const selectedFilePath = useSelector(
    workspaceStore,
    (s) => s.selectedFilePath
  );

  const route = routes.find((item) => item.id === activeRouteId);
  const files = filesByRoute[activeRouteId] ?? [];

  useEffect(() => {
    if (route?.workPath && route.workPath !== ".") {
      void loadWorkspaceFiles(route.id, route.workPath);
    }
  }, [route]);

  const handleRefresh = useCallback(() => {
    if (route?.workPath && route.workPath !== ".") {
      void loadWorkspaceFiles(route.id, route.workPath);
    }
  }, [route]);
  const runningTask = tasks.find(
    (task) => task.routeId === activeRouteId && task.status === "running"
  );
  const selectedFile = findFileNode(files, selectedFilePath);
  const selectedChangeMeta = selectedFile?.change
    ? CHANGE_BADGE[selectedFile.change]
    : null;

  const handleCiteFile = useCallback(() => {
    if (!selectedFile) {
      return;
    }
    addWorkspaceFileReference({
      path: selectedFile.path,
      name: selectedFile.name,
      isDir: selectedFile.kind === "dir",
      ...(selectedFile.sizeLabel ? { sizeLabel: selectedFile.sizeLabel } : {}),
    });
  }, [selectedFile]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-border-subtle flex items-center justify-between gap-2 border-b px-3.5 py-2.5">
        <span className="text-text-secondary truncate font-mono text-xs font-medium">
          {route ? pathBasename(route.workPath) : "工作区"}
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {route ? (
            <Button
              aria-label="刷新文件树"
              onClick={handleRefresh}
              size="icon"
              variant="ghost"
            >
              <Icon icon={Refresh01Icon} size="xs" />
            </Button>
          ) : null}
          {runningTask ? (
            <Badge
              tone={runningTask.lockMode === "exclusive" ? "amber" : "sky"}
            >
              <Icon
                icon={
                  runningTask.lockMode === "exclusive" ? LockIcon : LockOpenIcon
                }
                size="xs"
              />
              <span>
                {runningTask.lockMode === "exclusive" ? "写锁" : "读锁"} ·{" "}
                {runningTask.taskCode}
              </span>
            </Badge>
          ) : (
            <Badge tone="neutral">空闲</Badge>
          )}
        </div>
      </div>

      <ScrollArea className="relative flex min-h-0 flex-1 flex-col">
        <ScrollAreaViewport className="scrollbar-hide h-full w-full overflow-y-auto p-2.5">
          <ScrollAreaContent>
            {files.length === 0 ? (
              <div className="border-border-subtle text-text-muted rounded-lg border border-dashed px-3 py-6 text-center text-xs">
                暂无文件
              </div>
            ) : (
              <Tree
                aria-label="工作区文件树"
                className="flex flex-col gap-0.5 outline-none"
                defaultExpanded={[
                  "src",
                  "src/components",
                  "scripts",
                  "src/agent",
                ]}
                onValueChange={selectFile}
                value={selectedFilePath}
              >
                <WorkspaceFileRows nodes={files} />
              </Tree>
            )}
          </ScrollAreaContent>
        </ScrollAreaViewport>
        <ScrollAreaScrollbar
          className="absolute inset-y-1 right-0.5 w-1.5 opacity-0 transition-opacity data-visible:opacity-100"
          orientation="vertical"
        >
          <ScrollAreaThumb className="bg-border w-full rounded-full" />
        </ScrollAreaScrollbar>
      </ScrollArea>

      {selectedFile ? (
        <div className="border-border-subtle bg-surface/80 flex shrink-0 items-center justify-between gap-2 border-t px-3.5 py-2">
          <div className="min-w-0 flex-1">
            <div className="text-text truncate font-mono text-xs font-medium">
              {selectedFile.path}
            </div>
            <div className="text-text-muted mt-0.5 flex items-center gap-1.5 text-[11px]">
              <span>{selectedFile.sizeLabel ?? "目录"}</span>
              {selectedFile.touchedBy ? (
                <span>· {selectedFile.touchedBy}</span>
              ) : null}
              {selectedChangeMeta ? (
                <Badge tone={selectedChangeMeta.tone}>
                  {selectedChangeMeta.text}
                </Badge>
              ) : null}
            </div>
          </div>
          <Button onClick={handleCiteFile} size="xs" variant="subtle">
            <Icon icon={Link01Icon} size="xs" />
            <span>引用</span>
          </Button>
        </div>
      ) : null}

      <ArtifactList />
    </div>
  );
}
