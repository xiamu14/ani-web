import type { Transition } from "motion/react";

export const motionEase = [0.22, 1, 0.36, 1] as const;

export const motionTransition = {
  fast: { duration: 0.14, ease: motionEase },
  base: { duration: 0.2, ease: motionEase },
  slow: { duration: 0.3, ease: motionEase },
} satisfies Record<string, Transition>;

export const motionSpring = {
  snappy: { type: "spring", stiffness: 460, damping: 34, mass: 0.7 },
  gentle: { type: "spring", stiffness: 300, damping: 28, mass: 1 },
} satisfies Record<string, Transition>;
