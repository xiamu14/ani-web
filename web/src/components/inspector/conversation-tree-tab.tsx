import {
  CodeIcon,
  GitBranchIcon,
  HierarchyIcon,
  Route01Icon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import type { Edge, Node, NodeMouseHandler, NodeProps } from "@xyflow/react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
} from "@xyflow/react";
import { useCallback, useMemo } from "react";
import { Badge } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import { formatTokens } from "@/lib/format.ts";
import type { ConversationTreeNode } from "@/schemas/workspace.ts";
import { connectionStore } from "@/store/connection.ts";
import {
  activateTreeNode,
  forkActiveRoute,
  selectTreeNode,
  workspaceStore,
} from "@/store/workspace.ts";

interface TreeFlowData extends Record<string, unknown> {
  isCurrentThread: boolean;
  isSelected: boolean;
  kind: ConversationTreeNode["kind"];
  label: string;
  status: ConversationTreeNode["status"];
  tokens: number;
}

type TreeFlowNodeType = Node<TreeFlowData, "branch">;

const NODE_DOT: Record<ConversationTreeNode["status"], string> = {
  active: "bg-emerald-500",
  running: "animate-pulse bg-sky-500",
  queued: "bg-amber-500",
  completed: "bg-emerald-500",
  idle: "bg-text-muted/40",
};

const NODE_ICON: Record<ConversationTreeNode["kind"], typeof HierarchyIcon> = {
  root: HierarchyIcon,
  route: Route01Icon,
  task: CodeIcon,
};

const PRO_OPTIONS = { hideAttribution: true };

function TreeFlowCardNode({ data }: NodeProps<TreeFlowNodeType>) {
  return (
    <div
      className={cn(
        "bg-surface flex min-w-40 items-center justify-between gap-2 rounded-xl border px-3 py-2 text-xs shadow-xs transition-colors",
        data.isSelected
          ? "border-accent ring-accent/20 ring-2"
          : "border-border-subtle"
      )}
    >
      <Handle
        className="bg-border! size-1.5!"
        position={Position.Left}
        type="target"
      />
      <div className="flex min-w-0 items-center gap-1.5">
        <span
          className={cn(
            "size-1.5 shrink-0 rounded-full",
            NODE_DOT[data.status]
          )}
        />
        <Icon
          className={data.isCurrentThread ? "text-accent" : "text-text-muted"}
          icon={NODE_ICON[data.kind]}
          size="xs"
        />
        <span className="text-text max-w-28 truncate font-medium">
          {data.label}
        </span>
        {data.isCurrentThread ? <Badge tone="accent">当前</Badge> : null}
      </div>
      <span className="text-text-muted shrink-0 font-mono text-[10px] tabular-nums">
        {formatTokens(data.tokens)}
      </span>
      <Handle
        className="bg-border! size-1.5!"
        position={Position.Right}
        type="source"
      />
    </div>
  );
}

const NODE_TYPES = {
  branch: TreeFlowCardNode,
};

function findNodeById(
  nodes: readonly ConversationTreeNode[],
  id: string
): ConversationTreeNode | null {
  for (const node of nodes) {
    if (node.id === id) {
      return node;
    }
    if (node.children) {
      const hit = findNodeById(node.children, id);
      if (hit) {
        return hit;
      }
    }
  }
  return null;
}

function buildFlowGraph(
  roots: readonly ConversationTreeNode[],
  selectedId: string,
  activeThreadId: string
): { nodes: TreeFlowNodeType[]; edges: Edge[] } {
  const nodes: TreeFlowNodeType[] = [];
  const edges: Edge[] = [];
  let rowIndex = 0;

  function visit(
    node: ConversationTreeNode,
    depth: number,
    parentId: string | null
  ): number {
    const children = node.children ?? [];
    let yCenter: number;

    if (children.length === 0) {
      yCenter = rowIndex * 74;
      rowIndex += 1;
    } else {
      const childYs = children.map((child) => visit(child, depth + 1, node.id));
      const firstY = childYs[0] ?? 0;
      const lastY = childYs.at(-1) ?? firstY;
      yCenter = (firstY + lastY) / 2;
    }

    nodes.push({
      id: node.id,
      type: "branch",
      position: { x: depth * 210, y: yCenter },
      data: {
        label: node.label,
        kind: node.kind,
        status: node.status,
        tokens: node.tokens,
        isCurrentThread: node.refId === activeThreadId,
        isSelected: node.id === selectedId,
      },
    });

    if (parentId) {
      edges.push({
        id: `edge-${parentId}-${node.id}`,
        source: parentId,
        target: node.id,
        animated: node.status === "running" || node.status === "active",
      });
    }

    return yCenter;
  }

  for (const root of roots) {
    visit(root, 0, null);
  }

  return { nodes, edges };
}

function BranchActions() {
  const status = useSelector(connectionStore, (s) => s.status);

  const handleFork = useCallback(() => {
    void forkActiveRoute("full_fork");
  }, []);

  const handleHandoff = useCallback(() => {
    void forkActiveRoute("handoff");
  }, []);

  if (status !== "connected") {
    return null;
  }

  return (
    <div className="flex items-center gap-1">
      <Button onClick={handleFork} size="xs" variant="subtle">
        分叉
      </Button>
      <Button onClick={handleHandoff} size="xs" variant="subtle">
        交接
      </Button>
    </div>
  );
}

export function ConversationTreeTab() {
  const treeNodes = useSelector(workspaceStore, (s) => s.treeNodes);
  const selectedId = useSelector(workspaceStore, (s) => s.selectedTreeNodeId);
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const activeTaskId = useSelector(workspaceStore, (s) => s.activeTaskId);
  const activeThreadId = activeTaskId ?? activeRouteId;

  const selected = findNodeById(treeNodes, selectedId);

  const { nodes, edges } = useMemo(
    () => buildFlowGraph(treeNodes, selectedId, activeThreadId),
    [treeNodes, selectedId, activeThreadId]
  );

  const handleNodeClick: NodeMouseHandler<TreeFlowNodeType> = useCallback(
    (_event, node) => {
      selectTreeNode(node.id);
    },
    []
  );

  const handleSwitchSession = useCallback(() => {
    if (selected && selected.kind === "route") {
      activateTreeNode(selected.kind, selected.refId);
    }
  }, [selected]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-border-subtle flex items-center justify-between border-b px-3.5 py-2.5">
        <span className="text-text-secondary inline-flex items-center gap-1.5 text-xs font-medium">
          <Icon icon={GitBranchIcon} size="xs" />
          <span>分支结构</span>
        </span>
        <BranchActions />
      </div>

      <div className="relative min-h-0 flex-1">
        {nodes.length === 0 ? (
          <div className="p-3">
            <div className="border-border-subtle text-text-muted rounded-lg border border-dashed px-3 py-6 text-center text-xs">
              暂无分支
            </div>
          </div>
        ) : (
          <ReactFlow
            edges={edges}
            fitView
            maxZoom={1.5}
            minZoom={0.4}
            nodes={nodes}
            nodesConnectable={false}
            nodesDraggable={false}
            nodeTypes={NODE_TYPES}
            onNodeClick={handleNodeClick}
            proOptions={PRO_OPTIONS}
          >
            <Background gap={16} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>

      {selected ? (
        <div className="border-border-subtle bg-surface/80 flex shrink-0 items-center justify-between gap-2 border-t px-3.5 py-2.5">
          <div className="min-w-0">
            <div className="text-text truncate text-xs font-medium">
              {selected.label}
            </div>
            <div className="text-text-muted mt-0.5 font-mono text-[11px]">
              {selected.kind === "task"
                ? `子会话 ${selected.entryId.slice(0, 8)}`
                : `HEAD ${selected.entryId.slice(0, 8)} · ${formatTokens(selected.tokens)} tokens`}
            </div>
          </div>
          {selected.kind === "route" ? (
            <Button onClick={handleSwitchSession} size="xs" variant="subtle">
              切换会话
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
