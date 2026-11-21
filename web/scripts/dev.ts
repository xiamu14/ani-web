import { finish, integer, parseCommand, runCli, start } from "./shared/cli.js";
import { tool } from "./shared/process.js";
import { booleanValue, textValue } from "./shared/prompts.js";

await runCli("dev", async () => {
  const args = parseCommand({
    name: "dev",
    description: "本地开发：启动 Vite + TanStack Start SPA 开发服务器。",
    options: {
      port: {
        type: "string",
        default: process.env.PORT ?? "5173",
        description: "HTTP 端口，范围 1–65535；也可使用 PORT",
      },
      host: {
        type: "boolean",
        default: false,
        description: "监听所有网络接口（0.0.0.0）",
      },
      open: {
        type: "boolean",
        default: false,
        description: "启动后自动打开浏览器",
      },
    },
    examples: [
      "bun run dev",
      "bun run dev --port 3000",
      "bun run dev --host --open",
      "bun run dev --interactive",
    ],
  });
  if (!args) {
    return;
  }
  let port = String(args.values.port);
  let host = args.values.host === true;
  let open = args.values.open === true;
  if (args.interactive) {
    if (!args.provided.has("port")) {
      port = await textValue("HTTP 端口", port);
    }
    if (!args.provided.has("host")) {
      host = await booleanValue("监听所有网络接口？", host);
    }
    if (!args.provided.has("open")) {
      open = await booleanValue("自动打开浏览器？", open);
    }
  }
  const validatedPort = integer(port, "--port", 65_535);
  start(`本地开发 · http://localhost:${validatedPort}`);
  await tool(
    "vite",
    [
      "dev",
      "--port",
      String(validatedPort),
      "--strictPort",
      ...(host ? ["--host"] : []),
      ...(open ? ["--open"] : []),
    ],
    {
      NODE_ENV: "development",
      PORT: String(validatedPort),
    }
  );
  finish("开发服务已停止");
});
