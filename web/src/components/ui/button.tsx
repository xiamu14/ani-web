import { Button as SdkButton } from "@ui-sdk/react/button";
import type { VariantProps } from "class-variance-authority";
import { cva } from "class-variance-authority";
import type { ComponentProps } from "react";
import { cn } from "@/lib/cn.ts";

export const buttonVariants = cva(
  "focus-visible:ring-accent/40 inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-lg font-medium whitespace-nowrap transition-colors outline-none select-none focus-visible:ring-2 disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        primary: "bg-accent hover:bg-accent/90 text-white shadow-xs",
        subtle:
          "bg-surface-subtle text-text hover:bg-surface-hover border-border-subtle border",
        outline:
          "border-border bg-surface text-text hover:bg-surface-subtle border",
        ghost:
          "text-text-secondary hover:bg-surface-hover hover:text-text bg-transparent",
      },
      size: {
        xs: "h-6.5 px-2 text-xs",
        sm: "h-7.5 px-2.5 text-xs",
        md: "h-8.5 px-3 text-sm",
        icon: "size-7.5",
      },
    },
    defaultVariants: {
      variant: "ghost",
      size: "sm",
    },
  }
);

type ButtonProps = ComponentProps<typeof SdkButton> &
  VariantProps<typeof buttonVariants>;

export function Button({ variant, size, className, ...props }: ButtonProps) {
  return (
    <SdkButton
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  );
}
