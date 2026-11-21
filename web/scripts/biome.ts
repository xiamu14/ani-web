import { log } from "@clack/prompts";
import { finish, parseCommand, runCli, start } from "./shared/cli.js";
import { tool } from "./shared/process.js";
import { booleanValue } from "./shared/prompts.js";

await runCli("biome", async () => {
  const args = parseCommand({
    name: "biome",
    description:
      "代码检查：默认修复 Tailwind 规范类名与 Biome 问题，再执行 TypeScript 类型检查；弃用 API 按 warning 报告，加 --check 仅检查。",
    options: {
      check: {
        type: "boolean",
        default: false,
        description: "只检查，不写入修复与格式化结果",
      },
      unsafe: {
        type: "boolean",
        default: false,
        description: "启用 unsafe 自动修复（不能与 --check 同用）",
      },
    },
    examples: [
      "bun run biome",
      "bun run biome --check",
      "bun run biome --unsafe",
      "bun run biome --interactive",
    ],
  });
  if (!args) {
    return;
  }

  let checkOnly = args.values.check === true;
  let unsafe = args.values.unsafe === true;
  if (args.interactive) {
    if (!args.provided.has("check")) {
      checkOnly = await booleanValue("仅检查（不写入修改）？", checkOnly);
    }
    if (!(checkOnly || args.provided.has("unsafe"))) {
      unsafe = await booleanValue("启用 unsafe 自动修复？", unsafe);
    }
  }
  if (unsafe && checkOnly) {
    throw new Error("--unsafe 不能与 --check 同时使用");
  }

  start(checkOnly ? "代码检查" : "代码检查与修复");
  log.step("Tailwind 规范类名检查");
  await tool("eslint", [
    "--config",
    "scripts/tailwind.config.mjs",
    "--max-warnings",
    "0",
    ...(checkOnly ? [] : ["--fix"]),
    "src",
  ]);
  log.step("Biome 检查与格式化");
  await tool("biome", [
    "check",
    ...(checkOnly ? [] : ["--write"]),
    ...(unsafe ? ["--unsafe"] : []),
    "src",
    "scripts",
    "biome.jsonc",
    "package.json",
  ]);
  const { runTypeCheck } = await import("./shared/type-check.js");
  await runTypeCheck();
  finish(checkOnly ? "代码检查通过" : "代码检查与修复完成");
});
