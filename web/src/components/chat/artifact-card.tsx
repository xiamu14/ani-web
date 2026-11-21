import {
  DragDropVerticalIcon,
  Link01Icon,
  PackageIcon,
} from "@hugeicons/core-free-icons";
import type { DragEvent } from "react";
import { useCallback } from "react";
import { Badge } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import type { DragRefPayload } from "@/lib/drag-ref.ts";
import { writeDragRef } from "@/lib/drag-ref.ts";
import type { ArtifactItem } from "@/schemas/workspace.ts";
import { addArtifactReference, setDraggingRef } from "@/store/composer.ts";

export function ArtifactCard({
  artifact,
  compact = false,
}: {
  readonly artifact: ArtifactItem;
  readonly compact?: boolean;
}) {
  const handleDragStart = useCallback(
    (event: DragEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      const payload: DragRefPayload = {
        kind: "artifact",
        artifactId: artifact.id,
        artifactCode: artifact.artifactCode,
        title: artifact.title,
        format: artifact.format,
        sizeLabel: artifact.sizeLabel,
      };
      writeDragRef(event.dataTransfer, payload);
      setDraggingRef({
        kind: "artifact",
        label: artifact.title,
        payload,
      });
    },
    [artifact]
  );

  const handleDragEnd = useCallback(() => {
    setDraggingRef(null);
  }, []);

  const handleCite = useCallback(() => {
    addArtifactReference(artifact);
  }, [artifact]);

  return (
    <div
      className={cn(
        "border-border-subtle bg-surface-subtle hover:border-accent/40 group flex items-center justify-between gap-2 rounded-xl border transition-colors",
        compact ? "px-2.5 py-2" : "px-3 py-2.5"
      )}
    >
      <button
        aria-label={`拖拽或引用产物 ${artifact.title}`}
        className="flex min-w-0 flex-1 cursor-grab items-start gap-2 text-left outline-none active:cursor-grabbing"
        draggable
        onClick={handleCite}
        onDragEnd={handleDragEnd}
        onDragStart={handleDragStart}
        type="button"
      >
        <span className="text-text-muted mt-0.5 flex shrink-0 items-center gap-1">
          <Icon
            className="opacity-50 transition-opacity group-hover:opacity-100"
            icon={DragDropVerticalIcon}
            size="xs"
          />
          <Icon className="text-accent" icon={PackageIcon} size="xs" />
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="text-text truncate text-xs font-medium">
              {artifact.title}
            </span>
            <Badge className="font-mono" tone="neutral">
              {artifact.format}
            </Badge>
          </span>
          <span className="text-text-muted mt-0.5 flex items-center gap-1.5 text-[11px]">
            <span className="truncate font-mono">{artifact.artifactCode}</span>
            <span>·</span>
            <span className="shrink-0">{artifact.sizeLabel}</span>
            {compact ? null : (
              <>
                <span>·</span>
                <span className="truncate">{artifact.summary}</span>
              </>
            )}
          </span>
        </span>
      </button>

      <Button onClick={handleCite} size="xs" variant="ghost">
        <Icon icon={Link01Icon} size="xs" />
        <span>引用</span>
      </Button>
    </div>
  );
}
