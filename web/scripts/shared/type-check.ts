import { log } from "@clack/prompts";
// biome-ignore lint/correctness/noUndeclaredDependencies: provided by @tanstack/react-start
import { Generator, getConfig } from "@tanstack/router-generator";
import { reportDeprecations } from "./deprecation-check.js";
import { runtimePath, tool, webRoot } from "./process.js";

export async function runTypeCheck(): Promise<void> {
  log.step("生成路由类型并执行 TypeScript 类型检查");
  const routerConfig = getConfig(
    {
      target: "react",
      routesDirectory: runtimePath("src/routes"),
      generatedRouteTree: runtimePath("src/routeTree.gen.ts"),
      enableRouteTreeFormatting: false,
    },
    webRoot
  );
  await new Generator({ config: routerConfig, root: webRoot }).run();
  await tool("tsc", ["--project", "tsconfig.json", "--noEmit"]);
  await tool("tsc", ["--project", "scripts/tsconfig.json"]);
  log.step("检查已弃用 API（warning）");
  await reportDeprecations(["tsconfig.json", "scripts/tsconfig.json"]);
}
