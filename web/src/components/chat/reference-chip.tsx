import {
  BotIcon,
  Cancel01Icon,
  File01Icon,
  FileAttachmentIcon,
  FileCodeIcon,
  FileTextIcon,
  Folder01Icon,
  Image01Icon,
  PackageIcon,
  Pdf01Icon,
} from "@hugeicons/core-free-icons";
import type { VariantProps } from "class-variance-authority";
import { cva } from "class-variance-authority";
import { useCallback } from "react";
import { Icon } from "@/components/ui/icon.tsx";
import { cn } from "@/lib/cn.ts";
import type { AttachmentKind, ComposerReference } from "@/schemas/workspace.ts";

const chipVariants = cva(
  "inline-flex max-w-full items-center gap-1.5 rounded-lg border px-2 py-1 text-xs transition-colors",
  {
    variants: {
      tone: {
        surface: "border-border-subtle bg-surface-subtle text-text",
        inverted: "border-white/25 bg-white/15 text-white",
      },
    },
    defaultVariants: {
      tone: "surface",
    },
  }
);

const ATTACHMENT_ICON: Record<AttachmentKind, typeof FileAttachmentIcon> = {
  image: Image01Icon,
  audio: FileAttachmentIcon,
  video: FileAttachmentIcon,
  document: Pdf01Icon,
  binary: FileAttachmentIcon,
};

function resolveFileIcon(name: string, isDir: boolean) {
  if (isDir) {
    return Folder01Icon;
  }
  if (name.endsWith(".md") || name.endsWith(".txt")) {
    return FileTextIcon;
  }
  if (
    name.endsWith(".ts") ||
    name.endsWith(".tsx") ||
    name.endsWith(".py") ||
    name.endsWith(".json") ||
    name.endsWith(".css")
  ) {
    return FileCodeIcon;
  }
  return File01Icon;
}

function resolveRefMeta(reference: ComposerReference): {
  icon: typeof FileAttachmentIcon;
  label: string;
  meta: string;
  tag: string;
} {
  if (reference.kind === "attachment") {
    return {
      icon: ATTACHMENT_ICON[reference.attachmentKind],
      label: reference.title,
      meta: reference.sizeLabel,
      tag: "附件",
    };
  }
  if (reference.kind === "file") {
    return {
      icon: resolveFileIcon(reference.name, reference.isDir),
      label: reference.path,
      meta: reference.sizeLabel ?? "目录",
      tag: "文件",
    };
  }
  if (reference.kind === "task") {
    const brief =
      reference.title.length > 24
        ? `${reference.title.slice(0, 24)}…`
        : reference.title;
    return {
      icon: BotIcon,
      label: `@${reference.role}(${reference.taskCode})`,
      meta: brief,
      tag: "任务",
    };
  }
  return {
    icon: PackageIcon,
    label: reference.title,
    meta: reference.artifactCode,
    tag: "产物",
  };
}

export function ReferenceChip({
  onRemove,
  reference,
  tone,
}: {
  readonly reference: ComposerReference;
  readonly onRemove?: (id: string) => void;
  readonly tone?: VariantProps<typeof chipVariants>["tone"];
}) {
  const { icon, label, meta, tag } = resolveRefMeta(reference);
  const handleRemove = useCallback(() => {
    onRemove?.(reference.id);
  }, [onRemove, reference.id]);

  return (
    <span
      className={cn(chipVariants({ tone }))}
      title={`${tag}: ${label} · ${meta}`}
    >
      <Icon
        className={tone === "inverted" ? "text-white/90" : "text-accent"}
        icon={icon}
        size="xs"
      />
      <span className="max-w-44 shrink-0 truncate font-medium">{label}</span>
      <span
        className={cn(
          "max-w-36 min-w-0 truncate font-mono text-[10px]",
          tone === "inverted" ? "text-white/75" : "text-text-muted"
        )}
      >
        {meta}
      </span>
      {onRemove ? (
        <button
          aria-label={`移除 ${label}`}
          className="text-text-muted hover:text-text ml-0.5 inline-flex size-4 shrink-0 cursor-pointer items-center justify-center rounded transition-colors"
          onClick={handleRemove}
          type="button"
        >
          <Icon icon={Cancel01Icon} size="xs" />
        </button>
      ) : null}
    </span>
  );
}
