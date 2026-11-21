import { isAbsolute, relative, resolve, sep } from "node:path";
import { log } from "@clack/prompts";
import type { APIOptions } from "typescript/unstable/async";
import { API } from "typescript/unstable/async";
import { webRoot } from "./process.js";

export interface DeprecationWarning {
  character: number;
  code: number;
  fileName: string;
  line: number;
  severity: "warning";
  text: string;
}

function isProjectSource(fileName: string, root: string): boolean {
  const path = relative(root, fileName);
  const parts = path.split(sep);
  return !(
    isAbsolute(path) ||
    parts.includes("..") ||
    parts.includes("node_modules") ||
    parts.includes(".tanstack") ||
    path.endsWith("routeTree.gen.ts")
  );
}

export async function findDeprecations(
  configFiles: readonly string[],
  options: APIOptions = {}
): Promise<DeprecationWarning[]> {
  const root = options.cwd ?? webRoot;
  const configs = configFiles.map((file) => resolve(root, file));
  const warnings = new Map<string, DeprecationWarning>();
  const api = new API({ ...options, cwd: root });
  try {
    const snapshot = await api.updateSnapshot({ openProjects: configs });
    try {
      await Promise.all(
        configs.map(async (config) => {
          const project = snapshot.getProject(config);
          if (!project) {
            throw new Error(`无法加载 TypeScript 项目：${config}`);
          }
          const diagnostics = await project.program.getSuggestionDiagnostics();
          await Promise.all(
            diagnostics.map(async (diagnostic) => {
              const { fileName } = diagnostic;
              if (
                !(
                  diagnostic.reportsDeprecated &&
                  fileName &&
                  isProjectSource(fileName, root)
                )
              ) {
                return;
              }
              const source = await project.program.getSourceFile(fileName);
              if (!source) {
                throw new Error(`无法读取弃用诊断位置：${fileName}`);
              }
              const position = source.getLineAndCharacterOfPosition(
                diagnostic.pos
              );
              const key = `${fileName}:${diagnostic.pos}:${diagnostic.code}`;
              warnings.set(key, {
                fileName,
                line: position.line + 1,
                character: position.character + 1,
                code: diagnostic.code,
                severity: "warning",
                text: diagnostic.text,
              });
            })
          );
        })
      );
    } finally {
      await snapshot.dispose();
    }
  } finally {
    await api.close();
  }
  return [...warnings.values()].sort(
    (left, right) =>
      left.fileName.localeCompare(right.fileName) ||
      left.line - right.line ||
      left.character - right.character
  );
}

export async function reportDeprecations(
  configFiles: readonly string[],
  options: APIOptions = {}
): Promise<number> {
  const warnings = await findDeprecations(configFiles, options);
  for (const warning of warnings) {
    log.warn(formatDeprecationWarning(warning));
  }
  log.info(`弃用 API：${warnings.length} 个 warning`);
  return warnings.length;
}

export function formatDeprecationWarning(warning: DeprecationWarning): string {
  const file = relative(webRoot, warning.fileName);
  return `${file}:${warning.line}:${warning.character} - ${warning.severity} TS${warning.code}: ${warning.text}`;
}
