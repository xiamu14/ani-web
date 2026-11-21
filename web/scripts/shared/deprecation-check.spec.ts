import { join } from "node:path";
import { log } from "@clack/prompts";
import { createVirtualFileSystem } from "typescript/unstable/fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findDeprecations, reportDeprecations } from "./deprecation-check.js";
import { webRoot } from "./process.js";

const root = join(webRoot, "scripts", "__deprecation-fixture__");

function fixture(source: string) {
  const config = JSON.stringify({
    compilerOptions: { noLib: true, noEmit: true, noUnusedLocals: true },
    files: ["source.ts"],
  });
  return {
    cwd: root,
    fs: createVirtualFileSystem({
      [join(root, "tsconfig.json")]: config,
      [join(root, "second.json")]: config,
      [join(root, "source.ts")]: source,
    }),
  };
}

afterEach(() => vi.restoreAllMocks());

describe("TypeScript 弃用警告", { timeout: 15_000 }, () => {
  it("识别弃用符号和调用签名，保留中文后的准确位置", async () => {
    const warnings = await findDeprecations(
      ["tsconfig.json", "second.json"],
      fixture(
        [
          "/** @deprecated Use current instead. */",
          "declare const oldValue: number;",
          "interface Client {",
          "  /** @deprecated Use query instead. */",
          "  fetch(): number;",
          "}",
          "declare const client: Client;",
          'const label = "中文"; oldValue; client.fetch();',
          "export {};",
        ].join("\n")
      )
    );
    expect(warnings).toHaveLength(2);
    expect(warnings).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: 6385,
          severity: "warning",
          line: 8,
          character: 21,
        }),
        expect.objectContaining({ code: 6387, severity: "warning", line: 8 }),
      ])
    );
  });

  it("忽略普通提示和未使用的弃用声明", async () => {
    const warnings = await findDeprecations(
      ["tsconfig.json"],
      fixture(
        [
          "/** @deprecated This declaration is intentionally unused. */",
          "declare const unused: number;",
          "async function current() { return 1; }",
          "current();",
          "export {};",
        ].join("\n")
      )
    );
    expect(warnings).toEqual([]);
  });

  it("按 warning 输出，不让只有弃用问题的检查失败", async () => {
    const warn = vi.spyOn(log, "warn").mockImplementation(() => undefined);
    vi.spyOn(log, "info").mockImplementation(() => undefined);
    const { exitCode } = process;
    await expect(
      reportDeprecations(
        ["tsconfig.json"],
        fixture(
          [
            "/** @deprecated Use current instead. */",
            "declare function old(): number;",
            "old();",
            "export {};",
          ].join("\n")
        )
      )
    ).resolves.toBe(1);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("source.ts:3:1 - warning TS6387:")
    );
    expect(process.exitCode).toBe(exitCode);
  });
});
