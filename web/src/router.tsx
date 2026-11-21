import { QueryClientProvider } from "@tanstack/react-query";
import { createRouter } from "@tanstack/react-router";
import { MotionConfig } from "motion/react";
import type { ReactNode } from "react";
import { queryClient } from "./lib/query-client.ts";
import { routeTree } from "./routeTree.gen.ts";

function RouterWrap({ children }: { readonly children: ReactNode }) {
  return (
    <MotionConfig reducedMotion="user">
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MotionConfig>
  );
}

export function getRouter() {
  return createRouter({
    routeTree,
    context: {
      queryClient,
    },
    defaultPreload: "intent",
    scrollRestoration: true,
    Wrap: RouterWrap,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>;
  }
}
