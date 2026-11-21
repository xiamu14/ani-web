import parser from "@babel/eslint-parser";
import tailwind from "eslint-plugin-better-tailwindcss";

export default [
  {
    ignores: ["**/routeTree.gen.ts", "**/.tanstack/**"],
  },
  ...["ts", "tsx"].map((extension) => ({
    files: [`src/**/*.${extension}`],
    languageOptions: {
      parser,
      parserOptions: {
        requireConfigFile: false,
        babelOptions: {
          babelrc: false,
          configFile: false,
          parserOpts: {
            plugins: ["typescript", ...(extension === "tsx" ? ["jsx"] : [])],
          },
        },
      },
    },
    plugins: { "better-tailwindcss": tailwind },
    settings: {
      "better-tailwindcss": {
        entryPoint: "src/styles.css",
        rootFontSize: 16,
      },
    },
    rules: {
      "better-tailwindcss/enforce-canonical-classes": [
        "error",
        { collapse: false, logical: false },
      ],
    },
  })),
];
