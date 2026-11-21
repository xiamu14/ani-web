import type { VariantProps } from "class-variance-authority";
import { cva } from "class-variance-authority";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn.ts";
import type { RoleName, TaskStatus } from "@/schemas/workspace.ts";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "bg-surface-subtle text-text-secondary",
        accent: "bg-accent-soft text-accent-fg",
        sky: "bg-sky-500/12 text-sky-700 dark:text-sky-300",
        emerald: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-300",
        amber: "bg-amber-500/12 text-amber-700 dark:text-amber-300",
        rose: "bg-rose-500/12 text-rose-700 dark:text-rose-300",
        violet: "bg-violet-500/12 text-violet-700 dark:text-violet-300",
      },
    },
    defaultVariants: {
      tone: "neutral",
    },
  }
);

type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

export const ROLE_META: Record<RoleName, { label: string; tone: BadgeTone }> = {
  coder: { label: "编码", tone: "accent" },
  reviewer: { label: "审查", tone: "violet" },
  tester: { label: "测试", tone: "amber" },
  researcher: { label: "调研", tone: "sky" },
};

export const STATUS_META: Record<
  TaskStatus,
  { label: string; tone: BadgeTone; dotClass: string }
> = {
  running: {
    label: "运行中",
    tone: "sky",
    dotClass: "bg-sky-500 animate-pulse",
  },
  queued: { label: "排队中", tone: "amber", dotClass: "bg-amber-500" },
  completed: { label: "已完成", tone: "emerald", dotClass: "bg-emerald-500" },
  failed: { label: "已失败", tone: "rose", dotClass: "bg-rose-500" },
  interrupted: { label: "已中断", tone: "rose", dotClass: "bg-rose-500" },
  cancelled: { label: "已取消", tone: "neutral", dotClass: "bg-text-muted" },
};

export function Badge({
  tone,
  className,
  children,
}: {
  readonly tone?: BadgeTone;
  readonly className?: string;
  readonly children: ReactNode;
}) {
  return (
    <span className={cn(badgeVariants({ tone }), className)}>{children}</span>
  );
}
