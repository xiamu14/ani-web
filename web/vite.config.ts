import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import viteReact from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const fallbackAgentUrl = process.env.VITE_AGENT_URL ?? "http://127.0.0.1:8765";
const runtimeJsonPath =
  process.env.AGENT_RUNTIME_JSON ??
  path.join(os.homedir(), ".agent", "runtime.json");

interface RuntimeDiscoveryInfo {
  auth_token?: string;
  http_url?: string;
  pid?: number;
  protocol_version?: string;
  started_at?: string;
  uds_path?: string;
}

function readRuntimeInfo(): RuntimeDiscoveryInfo | null {
  try {
    if (!fs.existsSync(runtimeJsonPath)) {
      return null;
    }
    const raw = fs.readFileSync(runtimeJsonPath, "utf8");
    return JSON.parse(raw) as RuntimeDiscoveryInfo;
  } catch {
    return null;
  }
}

function proxyToAgent(
  req: http.IncomingMessage,
  res: http.ServerResponse,
  info: RuntimeDiscoveryInfo | null
): void {
  const headers: http.OutgoingHttpHeaders = { ...req.headers };
  delete headers.host;
  headers.host = "localhost";

  const targetPath = req.url ?? "/v1/health";
  const udsPath = info?.uds_path;
  const useUds = Boolean(udsPath && fs.existsSync(udsPath));

  let requestOptions: http.RequestOptions;
  if (useUds && udsPath) {
    requestOptions = {
      socketPath: udsPath,
      path: targetPath,
      method: req.method,
      headers,
    };
  } else {
    const parsedUrl = new URL(info?.http_url ?? fallbackAgentUrl);
    requestOptions = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port ? Number(parsedUrl.port) : 80,
      path: targetPath,
      method: req.method,
      headers,
    };
  }

  const proxyReq = http.request(requestOptions, (proxyRes) => {
    res.writeHead(proxyRes.statusCode ?? 502, proxyRes.headers);
    proxyRes.pipe(res);
  });

  proxyReq.on("error", (err) => {
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          code: "PROXY_ERROR",
          message: err.message,
        })
      );
    } else {
      res.end();
    }
  });

  res.on("close", () => {
    if (!res.writableEnded) {
      proxyReq.destroy();
    }
  });

  if (req.method === "GET" || req.method === "HEAD") {
    proxyReq.end();
  } else {
    req.pipe(proxyReq);
  }
}

const IGNORED_DIRS = new Set([
  ".git",
  ".next",
  ".output",
  ".tanstack",
  ".venv",
  ".vinxi",
  "__pycache__",
  "dist",
  "node_modules",
]);

interface FileTreeNode {
  children?: FileTreeNode[];
  kind: "dir" | "file";
  name: string;
  path: string;
  sizeLabel?: string;
}

function formatByteSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function readWorkspaceTree(
  rootDir: string,
  currentDir: string,
  depth: number,
  budget: { remaining: number }
): FileTreeNode[] {
  if (depth > 3 || budget.remaining <= 0) {
    return [];
  }
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(currentDir, { withFileTypes: true });
  } catch {
    return [];
  }

  entries.sort((a, b) => {
    if (a.isDirectory() !== b.isDirectory()) {
      return a.isDirectory() ? -1 : 1;
    }
    return a.name.localeCompare(b.name);
  });

  const nodes: FileTreeNode[] = [];
  for (const entry of entries) {
    if (budget.remaining <= 0) {
      break;
    }
    if (entry.name.startsWith(".") && IGNORED_DIRS.has(entry.name)) {
      continue;
    }
    if (entry.isDirectory() && IGNORED_DIRS.has(entry.name)) {
      continue;
    }
    const fullPath = path.join(currentDir, entry.name);
    const relPath = path.relative(rootDir, fullPath) || entry.name;
    budget.remaining -= 1;

    if (entry.isDirectory()) {
      nodes.push({
        path: relPath,
        name: entry.name,
        kind: "dir",
        children: readWorkspaceTree(rootDir, fullPath, depth + 1, budget),
      });
    } else if (entry.isFile()) {
      let sizeLabel = "";
      try {
        const stat = fs.statSync(fullPath);
        sizeLabel = formatByteSize(stat.size);
      } catch {
        sizeLabel = "";
      }
      nodes.push({
        path: relPath,
        name: entry.name,
        kind: "file",
        ...(sizeLabel ? { sizeLabel } : {}),
      });
    }
  }
  return nodes;
}

function agentRuntimeDiscoveryPlugin(): Plugin {
  return {
    name: "agent-runtime-discovery",
    configureServer(server) {
      server.middlewares.use("/__agent/files", (req, res) => {
        res.setHeader("Content-Type", "application/json");
        const parsed = new URL(req.url ?? "/", "http://localhost");
        const target = parsed.searchParams.get("path")?.trim();
        if (!target) {
          res.end(JSON.stringify({ files: [] }));
          return;
        }
        try {
          const resolved = path.resolve(target);
          if (!fs.statSync(resolved).isDirectory()) {
            res.end(JSON.stringify({ files: [] }));
            return;
          }
          const files = readWorkspaceTree(resolved, resolved, 0, {
            remaining: 300,
          });
          res.end(JSON.stringify({ rootPath: resolved, files }));
        } catch {
          res.end(JSON.stringify({ files: [] }));
        }
      });

      server.middlewares.use("/__agent/runtime", (_req, res) => {
        const info = readRuntimeInfo();
        res.setHeader("Content-Type", "application/json");
        if (!(info && info.auth_token)) {
          res.end(JSON.stringify({ found: false }));
          return;
        }
        res.end(
          JSON.stringify({
            found: true,
            udsPath: info.uds_path ?? null,
            httpUrl: info.http_url ?? fallbackAgentUrl,
            authToken: info.auth_token,
            protocolVersion: info.protocol_version ?? "1.0",
            pid: info.pid ?? null,
            startedAt: info.started_at ?? null,
          })
        );
      });

      server.middlewares.use("/v1", (req, res) => {
        const restoredReq = req;
        restoredReq.url = `/v1${req.url ?? ""}`;
        proxyToAgent(restoredReq, res, readRuntimeInfo());
      });
    },
  };
}

export default defineConfig({
  resolve: {
    tsconfigPaths: true,
  },
  plugins: [
    agentRuntimeDiscoveryPlugin(),
    tailwindcss(),
    tanstackStart({
      spa: {
        enabled: true,
      },
    }),
    viteReact(),
  ],
});
