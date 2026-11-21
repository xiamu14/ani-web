## Package management

- Use Bun as the only package manager for JavaScript and TypeScript projects in this directory, including `backend` and `web`.
- Install dependencies with `bun install`, add or remove packages with `bun add` / `bun remove`, and run package scripts with `bun run <script>`.
- Use `bunx` for package executables. Run the configured test script with `bun run test`; do not use the built-in `bun test` runner unless the project explicitly adopts it.
- Keep `bun.lock` as the dependency lockfile. Do not generate `package-lock.json`, `yarn.lock`, or `pnpm-lock.yaml`.

## Verification

- Only run the project's `bun run test` when changes touch or affect test cases; skip it for documentation, comments, or changes unrelated to test cases to avoid unnecessary test runs.
- When running `bun run test`, fix any errors; for warnings and coverage results, report them to the user only without attempting unsolicited fixes.
- All other AI Agent built-in automatic verification, testing, linters, formatters, or build checks are strictly prohibited unless explicitly requested by the user.

## Code organization

- Keep every code file under 500 lines. Aim for 200 lines or fewer; as a file approaches 200 lines, extract cohesive responsibilities into separate files and reuse them where appropriate.

## File Naming

- Use names that describe a feature or responsibility. Avoid project codenames and brand names for generic files, classes, variables, and log tags.

## UI

- All styling must use Tailwind CSS; do not create component/page CSS or style files (keep only the single Tailwind entry stylesheet) and do not use inline `style` attributes.
- Implement component style variants using `cva` (`class-variance-authority`) combined with `tailwind-merge` (`cn`).
- Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.

## State Management

- Use `@tanstack/react-store` for shared client state management; keep server state in TanStack Query and purely local UI state within the owning component.
- Strictly prohibit prop drilling (passing props down through multiple intermediate React components).
- Use state judiciously and minimize re-renders: derive values instead of duplicating state, keep state scoped as narrowly as possible, and always subscribe with fine-grained selectors in `useStore(store, (state) => ...)` rather than reading the entire store.

Like a master of Apple-style copywriting. Following the principles of _Don’t Make Me Think_ and _The Non-Designer’s Design Book_, rewrite all menu labels and feature copy in my admin dashboard. Make everything instantly understandable, use everyday language, and avoid jargon. Keep names to 2–4 Chinese characters and descriptions to no more than 20 Chinese characters. Don’t change any functionality. Ask me first if anything is unclear.
