import { Route01Icon } from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import { motion } from "motion/react";
import { useCallback } from "react";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import { pathBasename } from "@/lib/format.ts";
import { motionSpring } from "@/lib/motion.ts";
import type { RouteBranch } from "@/schemas/workspace.ts";
import { selectRoute, workspaceStore } from "@/store/workspace.ts";

const ROUTE_DOT: Record<RouteBranch["status"], string> = {
  active: "bg-emerald-500",
  idle: "bg-text-muted/50",
  waiting: "bg-amber-500",
};

function routeButtonTone(isSelected: boolean, isCurrentRoute: boolean): string {
  if (isSelected) {
    return "text-text";
  }
  if (isCurrentRoute) {
    return "bg-surface/60 text-text";
  }
  return "text-text-secondary hover:bg-surface-hover hover:text-text";
}

function RouteItemButton({
  isCurrentRoute,
  isSelected,
  route,
}: {
  readonly route: RouteBranch;
  readonly isSelected: boolean;
  readonly isCurrentRoute: boolean;
}) {
  const handleClick = useCallback(() => selectRoute(route.id), [route.id]);

  return (
    <li>
      <button
        className={cn(
          "group focus-visible:ring-accent/40 relative flex w-full cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors outline-none focus-visible:ring-2",
          routeButtonTone(isSelected, isCurrentRoute)
        )}
        onClick={handleClick}
        type="button"
      >
        {isSelected ? (
          <motion.span
            className="bg-surface border-border-subtle absolute inset-0 rounded-lg border shadow-xs"
            layoutId="active-route-bg"
            transition={motionSpring.snappy}
          />
        ) : null}
        <span className="bg-surface-subtle relative z-10 flex size-6 shrink-0 items-center justify-center rounded-md">
          <Icon
            className={isCurrentRoute ? "text-accent" : "text-text-muted"}
            icon={Route01Icon}
            size="xs"
          />
        </span>
        <span className="relative z-10 min-w-0 flex-1">
          <span className="flex items-center justify-between gap-1.5">
            <span className="truncate text-[13px] font-medium">
              {route.title}
            </span>
            <span
              className={cn(
                "size-1.5 shrink-0 rounded-full",
                ROUTE_DOT[route.status]
              )}
            />
          </span>
          <span className="text-text-muted mt-0.5 flex items-center justify-between gap-2 text-[11px]">
            <span className="truncate font-mono">
              {pathBasename(route.workPath)}
            </span>
            <span className="shrink-0">{route.updatedAt}</span>
          </span>
        </span>
      </button>
    </li>
  );
}

export function RouteList() {
  const routes = useSelector(workspaceStore, (s) => s.routes);
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const activeTaskId = useSelector(workspaceStore, (s) => s.activeTaskId);

  return (
    <section aria-label="对话路由" className="flex flex-col gap-1">
      <div className="flex items-center justify-between px-2 pb-1">
        <span className="text-text-muted text-[11px] font-semibold tracking-wider uppercase">
          对话路由
        </span>
        <span className="text-text-muted font-mono text-[11px]">
          {routes.length}
        </span>
      </div>

      {routes.length === 0 ? (
        <div className="border-border-subtle text-text-muted rounded-lg border border-dashed px-3 py-4 text-center text-xs">
          暂无会话
        </div>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {routes.map((route) => (
            <RouteItemButton
              isCurrentRoute={route.id === activeRouteId}
              isSelected={route.id === activeRouteId && !activeTaskId}
              key={route.id}
              route={route}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
