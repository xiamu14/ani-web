import { log } from "@clack/prompts";
import { finish, parseCommand, runCli, start } from "./shared/cli.js";
import { tool } from "./shared/process.js";
import { booleanValue } from "./shared/prompts.js";

await runCli("test", async () => {
  const args = parseCommand({
    name: "test",
    description:
      "运行测试：默认执行一次单元测试，类型与代码检查使用 bun run biome。",
    options: {
      watch: {
        type: "boolean",
        default: false,
        description: "监听测试变化",
      },
      coverage: {
        type: "boolean",
        default: false,
        description: "生成覆盖率报告",
      },
    },
    examples: [
      "bun run test",
      "bun run test --watch",
      "bun run test --coverage",
    ],
  });
  if (!args) {
    return;
  }

  let watch = args.values.watch === true;
  let coverage = args.values.coverage === true;
  if (args.interactive) {
    if (!args.provided.has("watch")) {
      watch = await booleanValue("监听测试变化？", watch);
    }
    if (!args.provided.has("coverage")) {
      coverage = await booleanValue("生成覆盖率报告？", coverage);
    }
  }

  start("单元测试");
  log.step(watch ? "监听测试变化" : "运行测试");
  await tool("vitest", [
    watch ? "watch" : "run",
    "--config",
    "vitest.config.ts",
    ...(coverage
      ? ["--coverage", "--coverage.reportsDirectory", "coverage/unit"]
      : []),
  ]);
  finish("测试完成");
});
