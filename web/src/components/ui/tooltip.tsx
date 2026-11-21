import {
  Tooltip as SdkTooltip,
  TooltipContent as SdkTooltipContent,
  TooltipTrigger as SdkTooltipTrigger,
} from "@ui-sdk/react/tooltip";
import type { ReactElement, ReactNode } from "react";
import { cn } from "@/lib/cn.ts";

interface TooltipHintProps {
  readonly children: ReactElement;
  readonly label: string;
  readonly side?: "top" | "bottom" | "left" | "right";
}

export function TooltipHint({
  children,
  label,
  side = "top",
}: TooltipHintProps) {
  return (
    <SdkTooltip delay={350}>
      <SdkTooltipTrigger render={children} />
      <SdkTooltipContent
        className={cn(
          "bg-text text-bg rounded-md px-2 py-1 text-xs font-medium shadow-md",
          "transition-[opacity,scale] duration-150 data-ending-style:scale-95 data-ending-style:opacity-0 data-starting-style:scale-95 data-starting-style:opacity-0"
        )}
        side={side}
        sideOffset={6}
      >
        {label}
      </SdkTooltipContent>
    </SdkTooltip>
  );
}

export function TooltipText({
  trigger,
  content,
  side = "top",
}: {
  readonly trigger: ReactNode;
  readonly content: string;
  readonly side?: "top" | "bottom" | "left" | "right";
}) {
  return (
    <SdkTooltip delay={350}>
      <SdkTooltipTrigger className="inline-flex cursor-default items-center">
        {trigger}
      </SdkTooltipTrigger>
      <SdkTooltipContent
        className="bg-text text-bg rounded-md px-2 py-1 text-xs font-medium shadow-md"
        side={side}
        sideOffset={6}
      >
        {content}
      </SdkTooltipContent>
    </SdkTooltip>
  );
}
