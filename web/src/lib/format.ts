/**
 * Format token counts compactly (e.g. 136800 -> "136.8k").
 */
export function formatTokens(tokens: number): string {
  if (tokens >= 1000) {
    const k = tokens / 1000;
    return `${k >= 100 ? Math.round(k) : k.toFixed(1)}k`;
  }
  return String(tokens);
}

/**
 * Compute clamped integer or one-decimal percentage.
 */
export function formatPercent(used: number, max: number): number {
  if (max <= 0) {
    return 0;
  }
  const ratio = Math.min(Math.max(used / max, 0), 1);
  return Math.round(ratio * 1000) / 10;
}

const TRAILING_SLASH_REGEX = /\/+$/u;

/**
 * Extract the final path segment for compact directory badges.
 */
export function pathBasename(path: string): string {
  const cleaned = path.replace(TRAILING_SLASH_REGEX, "");
  const parts = cleaned.split("/");
  return parts.at(-1) || cleaned;
}
