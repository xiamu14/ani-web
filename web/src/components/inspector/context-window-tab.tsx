import { useSelector } from "@tanstack/react-store";
import {
  Progress,
  ProgressIndicator,
  ProgressLabel,
  ProgressTrack,
  ProgressValue,
} from "@ui-sdk/react/progress";
import {
  ScrollArea,
  ScrollAreaContent,
  ScrollAreaScrollbar,
  ScrollAreaThumb,
  ScrollAreaViewport,
} from "@ui-sdk/react/scroll-area";
import { motion } from "motion/react";
import { cn } from "@/lib/cn.ts";
import { formatPercent, formatTokens } from "@/lib/format.ts";
import { motionTransition } from "@/lib/motion.ts";
import type { ContextSegment } from "@/schemas/workspace.ts";
import { workspaceStore } from "@/store/workspace.ts";

const SEGMENT_TONES: Record<
  ContextSegment["key"],
  { dot: string; fill: string }
> = {
  system: { dot: "bg-indigo-500", fill: "fill-indigo-500" },
  history: { dot: "bg-sky-500", fill: "fill-sky-500" },
  tools: { dot: "bg-amber-500", fill: "fill-amber-500" },
  memory: { dot: "bg-emerald-500", fill: "fill-emerald-500" },
};

const RING_RADIUS = 46;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
const BAR_WIDTH = 280;

export function ContextWindowTab() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const activeTaskId = useSelector(workspaceStore, (s) => s.activeTaskId);
  const contextByThread = useSelector(workspaceStore, (s) => s.contextByThread);

  const threadId = activeTaskId ?? activeRouteId;
  const snapshot = contextByThread[threadId];

  if (!snapshot) {
    return (
      <div className="text-text-muted p-6 text-center text-xs">暂无数据</div>
    );
  }

  const ratio =
    snapshot.maxTokens > 0
      ? Math.min(snapshot.usedTokens / snapshot.maxTokens, 1)
      : 0;
  const percent = formatPercent(snapshot.usedTokens, snapshot.maxTokens);
  const dashOffset = RING_CIRCUMFERENCE * (1 - ratio);
  const remainingTokens = Math.max(snapshot.maxTokens - snapshot.usedTokens, 0);

  let cursorX = 0;
  const stackedRects = snapshot.segments.map((segment) => {
    const width =
      snapshot.maxTokens > 0
        ? (segment.tokens / snapshot.maxTokens) * BAR_WIDTH
        : 0;
    const x = cursorX;
    cursorX += width;
    return { ...segment, x, width };
  });

  const metrics = [
    { label: "请求次数", value: String(snapshot.requests) },
    { label: "输入用量", value: formatTokens(snapshot.inputTokens) },
    { label: "输出用量", value: formatTokens(snapshot.outputTokens) },
    { label: "工具次数", value: String(snapshot.toolCalls) },
  ];

  return (
    <ScrollArea className="relative flex min-h-0 flex-1 flex-col">
      <ScrollAreaViewport className="scrollbar-hide h-full w-full overflow-y-auto p-3.5">
        <ScrollAreaContent className="flex flex-col gap-4">
          <div className="border-border-subtle bg-surface rounded-2xl border p-4 shadow-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="text-text text-xs font-semibold">窗口用量</span>
              <span className="bg-surface-subtle text-text-secondary rounded-md px-2 py-0.5 text-[11px] font-medium">
                {snapshot.label}
              </span>
            </div>

            <div className="my-4 flex items-center justify-center">
              <div className="relative flex size-32 items-center justify-center">
                <svg
                  aria-label="上下文占用环形图"
                  className="size-full -rotate-90"
                  role="img"
                  viewBox="0 0 120 120"
                >
                  <circle
                    className="stroke-surface-hover fill-none"
                    cx="60"
                    cy="60"
                    r={RING_RADIUS}
                    strokeWidth="10"
                  />
                  <motion.circle
                    animate={{ strokeDashoffset: dashOffset }}
                    className="stroke-accent fill-none"
                    cx="60"
                    cy="60"
                    initial={{ strokeDashoffset: RING_CIRCUMFERENCE }}
                    r={RING_RADIUS}
                    strokeDasharray={RING_CIRCUMFERENCE}
                    strokeLinecap="round"
                    strokeWidth="10"
                    transition={motionTransition.slow}
                  />
                </svg>
                <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
                  <span className="text-text text-xl font-bold tracking-tight tabular-nums">
                    {percent}%
                  </span>
                  <span className="text-text-muted mt-0.5 font-mono text-[11px] tabular-nums">
                    {formatTokens(snapshot.usedTokens)} /{" "}
                    {formatTokens(snapshot.maxTokens)}
                  </span>
                </div>
              </div>
            </div>

            <Progress
              className="flex flex-col gap-1.5"
              max={snapshot.maxTokens}
              value={snapshot.usedTokens}
            >
              <div className="flex items-center justify-between text-[11px]">
                <ProgressLabel className="text-text-secondary">
                  总占用比例
                </ProgressLabel>
                <ProgressValue className="text-text font-mono font-medium tabular-nums" />
              </div>
              <ProgressTrack className="bg-surface-subtle h-2 w-full overflow-hidden rounded-full">
                <ProgressIndicator className="bg-accent h-full rounded-full transition-[width] duration-300" />
              </ProgressTrack>
            </Progress>
          </div>

          <div className="border-border-subtle bg-surface rounded-2xl border p-3.5 shadow-xs">
            <div className="mb-2.5 flex items-center justify-between">
              <span className="text-text text-xs font-semibold">构成明细</span>
              <span className="text-text-muted text-[11px]">
                当前线程上下文构成
              </span>
            </div>

            <svg
              aria-label="上下文分段占比条"
              className="bg-surface-subtle mb-3 h-2.5 w-full overflow-hidden rounded-full"
              preserveAspectRatio="none"
              role="img"
              viewBox={`0 0 ${BAR_WIDTH} 10`}
            >
              {stackedRects.map((rect) => (
                <rect
                  className={SEGMENT_TONES[rect.key].fill}
                  height={10}
                  key={rect.key}
                  width={Math.max(rect.width, 0)}
                  x={rect.x}
                  y={0}
                />
              ))}
            </svg>

            <ul className="flex flex-col gap-2">
              {snapshot.segments.map((segment) => (
                <li
                  className="flex items-center justify-between text-xs"
                  key={segment.key}
                >
                  <span className="flex items-center gap-2">
                    <span
                      className={cn(
                        "size-2 rounded-full",
                        SEGMENT_TONES[segment.key].dot
                      )}
                    />
                    <span className="text-text-secondary">{segment.label}</span>
                  </span>
                  <span className="text-text font-mono text-[11px] tabular-nums">
                    {formatTokens(segment.tokens)} (
                    {formatPercent(segment.tokens, snapshot.maxTokens)}%)
                  </span>
                </li>
              ))}
              <li className="border-border-subtle flex items-center justify-between border-t pt-2 text-xs">
                <span className="flex items-center gap-2">
                  <span className="bg-border size-2 rounded-full" />
                  <span className="text-text-muted">剩余空间</span>
                </span>
                <span className="text-text-muted font-mono text-[11px] tabular-nums">
                  {formatTokens(remainingTokens)}
                </span>
              </li>
            </ul>
          </div>

          <div className="grid grid-cols-2 gap-2">
            {metrics.map((metric) => (
              <div
                className="border-border-subtle bg-surface rounded-xl border px-3 py-2.5 shadow-xs"
                key={metric.label}
              >
                <div className="text-text-muted text-[11px]">
                  {metric.label}
                </div>
                <div className="text-text mt-1 font-mono text-sm font-semibold tabular-nums">
                  {metric.value}
                </div>
              </div>
            ))}
          </div>
        </ScrollAreaContent>
      </ScrollAreaViewport>
      <ScrollAreaScrollbar
        className="absolute inset-y-1 right-0.5 w-1.5 opacity-0 transition-opacity data-visible:opacity-100"
        orientation="vertical"
      >
        <ScrollAreaThumb className="bg-border w-full rounded-full" />
      </ScrollAreaScrollbar>
    </ScrollArea>
  );
}
