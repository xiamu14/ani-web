import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  HeadContent,
  Outlet,
  Scripts,
} from "@tanstack/react-router";
import { useThemeSync } from "@/hooks/use-theme.ts";
import { themeBootstrapScript } from "@/lib/theme.ts";
import appCss from "@/styles.css?url";

export interface RouterContext {
  readonly queryClient: QueryClient;
}

export const Route = createRootRouteWithContext<RouterContext>()({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { name: "color-scheme", content: "light dark" },
      { title: "Agent 工作台" },
    ],
    links: [
      { rel: "icon", type: "image/svg+xml", href: "/favicon.svg" },
      { rel: "stylesheet", href: appCss },
    ],
  }),
  component: RootDocument,
});

function RootDocument() {
  useThemeSync();

  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script>{themeBootstrapScript}</script>
        <HeadContent />
      </head>
      <body className="bg-bg text-text h-dvh overflow-hidden font-sans antialiased">
        <Outlet />
        <Scripts />
      </body>
    </html>
  );
}
