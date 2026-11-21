import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    tsconfigPaths: true,
  },
  test: {
    globals: true,
    root: "./",
    include: ["src/**/*.spec.ts", "src/**/*.spec.tsx", "scripts/**/*.spec.ts"],
    coverage: {
      include: ["src/**/*.ts", "src/**/*.tsx", "scripts/**/*.ts"],
      exclude: ["src/routeTree.gen.ts", "scripts/**/*.spec.ts"],
    },
  },
});
