import type { IconSvgElement } from "@hugeicons/react";
import { HugeiconsIcon } from "@hugeicons/react";
import type { VariantProps } from "class-variance-authority";
import { cva } from "class-variance-authority";
import { cn } from "@/lib/cn.ts";

const iconVariants = cva("shrink-0", {
  variants: {
    size: {
      xs: "size-3.5",
      sm: "size-4",
      md: "size-4.5",
      lg: "size-5",
    },
  },
  defaultVariants: {
    size: "sm",
  },
});

interface IconProps extends VariantProps<typeof iconVariants> {
  readonly className?: string;
  readonly icon: IconSvgElement;
  readonly strokeWidth?: number;
}

export function Icon({ icon, size, strokeWidth = 1.75, className }: IconProps) {
  return (
    <HugeiconsIcon
      aria-hidden="true"
      className={cn(iconVariants({ size }), className)}
      icon={icon}
      strokeWidth={strokeWidth}
    />
  );
}
