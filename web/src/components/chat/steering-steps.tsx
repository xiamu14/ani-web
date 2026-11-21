import {
  AlertCircleIcon,
  ArrowRight01Icon,
  CheckIcon,
  Clock01Icon,
} from "@hugeicons/core-free-icons";
import {
  Collapsible,
  CollapsiblePanel,
  CollapsibleTrigger,
} from "@ui-sdk/react/collapsible";
import { Icon } from "@/components/ui/icon.tsx";
import type { TurnStep } from "@/schemas/workspace.ts";
import { ToolPayloadView } from "./tool-payload-view.tsx";

function StepProgressCircle({
  status,
  isRunning,
}: {
  readonly status?: TurnStep["status"];
  readonly isRunning: boolean;
}) {
  if (isRunning || status === "running") {
    return (
      <span
        aria-hidden="true"
        className="border-accent size-3.5 shrink-0 animate-spin rounded-full border-2 border-t-transparent"
      />
    );
  }
  if (status === "error") {
    return (
      <span className="flex size-3.5 shrink-0 items-center justify-center rounded-full bg-rose-500/15 text-rose-500">
        <Icon icon={AlertCircleIcon} size="xs" />
      </span>
    );
  }
  return (
    <span className="flex size-3.5 shrink-0 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-500">
      <Icon icon={CheckIcon} size="xs" />
    </span>
  );
}

function SteeringStepRow({
  step,
  isLast,
  isStreaming,
}: {
  readonly step: TurnStep;
  readonly isLast: boolean;
  readonly isStreaming: boolean;
}) {
  const isThinking = step.kind === "thinking";
  const isRunning =
    isStreaming && isLast && (isThinking || step.status === "running");
  const title = isThinking ? "思考过程" : (step.toolName ?? "tool");
  const hasDetail = Boolean(step.text || step.argsText || step.resultText);

  return (
    <div className="relative flex gap-2.5">
      <div className="flex flex-col items-center pt-0.5">
        <StepProgressCircle isRunning={isRunning} status={step.status} />
        {isLast ? null : <span className="bg-border-subtle mt-1 w-px flex-1" />}
      </div>

      <div className="min-w-0 flex-1 pb-2">
        {hasDetail ? (
          <Collapsible>
            <CollapsibleTrigger className="group flex w-full cursor-pointer items-center justify-between gap-2 text-left text-xs">
              <span className="text-text-secondary group-hover:text-text inline-flex min-w-0 items-center gap-1 font-medium">
                <Icon
                  className="shrink-0 transition-transform group-data-open:rotate-90"
                  icon={ArrowRight01Icon}
                  size="xs"
                />
                <span className="truncate font-mono">{title}</span>
              </span>
              {typeof step.durationMs === "number" && step.durationMs > 0 ? (
                <span className="text-text-muted inline-flex shrink-0 items-center gap-1 font-mono text-[11px]">
                  <Icon icon={Clock01Icon} size="xs" />
                  {step.durationMs}ms
                </span>
              ) : null}
            </CollapsibleTrigger>
            <CollapsiblePanel className="mt-1.5 flex flex-col gap-1.5">
              {step.text ? (
                <div className="text-text-muted text-xs leading-relaxed whitespace-pre-wrap">
                  {step.text}
                </div>
              ) : null}
              {step.argsText ? (
                <ToolPayloadView content={step.argsText} />
              ) : null}
              {step.resultText ? (
                <ToolPayloadView content={step.resultText} />
              ) : null}
            </CollapsiblePanel>
          </Collapsible>
        ) : (
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="text-text-secondary truncate font-mono font-medium">
              {title}
            </span>
            {typeof step.durationMs === "number" && step.durationMs > 0 ? (
              <span className="text-text-muted inline-flex shrink-0 items-center gap-1 font-mono text-[11px]">
                <Icon icon={Clock01Icon} size="xs" />
                {step.durationMs}ms
              </span>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

export function SteeringSteps({
  steps,
  streaming,
}: {
  readonly steps: readonly TurnStep[];
  readonly streaming?: boolean;
}) {
  if (steps.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-col">
      {steps.map((step, index) => (
        <SteeringStepRow
          isLast={index === steps.length - 1}
          isStreaming={Boolean(streaming)}
          key={step.id}
          step={step}
        />
      ))}
    </div>
  );
}
