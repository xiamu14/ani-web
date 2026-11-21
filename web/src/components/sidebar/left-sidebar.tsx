import {
  Add01Icon,
  Moon02Icon,
  SparklesIcon,
  Sun03Icon,
} from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import {
  ScrollArea,
  ScrollAreaContent,
  ScrollAreaScrollbar,
  ScrollAreaThumb,
  ScrollAreaViewport,
} from "@ui-sdk/react/scroll-area";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { TooltipHint } from "@/components/ui/tooltip.tsx";
import { cycleThemePreference, themeStore } from "@/store/theme.ts";
import { createMockRoute } from "@/store/workspace.ts";
import { ConnectionPopover } from "./connection-popover.tsx";
import { RoleTaskList } from "./role-task-list.tsx";
import { RouteList } from "./route-list.tsx";

export function LeftSidebar() {
  const preference = useSelector(themeStore, (s) => s.preference);

  return (
    <div className="flex h-full w-67 flex-col">
      <header className="border-border-subtle flex h-12 shrink-0 items-center justify-between border-b px-3">
        <div className="flex items-center gap-2">
          <span className="bg-accent flex size-6 items-center justify-center rounded-lg text-white">
            <Icon icon={SparklesIcon} size="xs" />
          </span>
          <span className="text-sm font-semibold tracking-tight">工作台</span>
          <ConnectionPopover />
        </div>

        <div className="flex items-center gap-1">
          <TooltipHint label="切换外观" side="bottom">
            <Button
              aria-label="切换外观"
              onClick={cycleThemePreference}
              size="icon"
              variant="ghost"
            >
              <Icon
                icon={preference === "dark" ? Moon02Icon : Sun03Icon}
                size="sm"
              />
            </Button>
          </TooltipHint>

          <TooltipHint label="新建会话" side="bottom">
            <Button
              aria-label="新建会话"
              onClick={createMockRoute}
              size="icon"
              variant="subtle"
            >
              <Icon icon={Add01Icon} size="sm" />
            </Button>
          </TooltipHint>
        </div>
      </header>

      <ScrollArea className="relative flex min-h-0 flex-1 flex-col">
        <ScrollAreaViewport className="scrollbar-hide h-full w-full overflow-y-auto px-2.5 py-3">
          <ScrollAreaContent className="flex flex-col gap-5">
            <RouteList />
            <RoleTaskList />
          </ScrollAreaContent>
        </ScrollAreaViewport>
        <ScrollAreaScrollbar
          className="absolute inset-y-1 right-0.5 w-1.5 opacity-0 transition-opacity data-visible:opacity-100"
          orientation="vertical"
        >
          <ScrollAreaThumb className="bg-border w-full rounded-full" />
        </ScrollAreaScrollbar>
      </ScrollArea>
    </div>
  );
}
