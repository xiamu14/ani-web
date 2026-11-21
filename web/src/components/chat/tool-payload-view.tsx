import type { ReactNode } from "react";

const JSON_TOKEN_REGEX =
  /(?<token>"(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/gu;

function tryFormatJson(raw: string): string | null {
  const trimmed = raw.trim();
  if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(trimmed);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return null;
  }
}

function getTokenClassName(token: string): string {
  if (token.startsWith('"')) {
    return token.endsWith(":")
      ? "text-sky-600 dark:text-sky-400"
      : "text-emerald-600 dark:text-emerald-400";
  }
  if (token === "true" || token === "false" || token === "null") {
    return "text-purple-600 dark:text-purple-400";
  }
  return "text-amber-600 dark:text-amber-400";
}

function renderColorizedJson(formatted: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let lastIndex = 0;

  for (const match of formatted.matchAll(JSON_TOKEN_REGEX)) {
    const index = match.index ?? 0;
    const [token] = match;
    if (index > lastIndex) {
      nodes.push(
        <span className="text-text-muted" key={`p-${lastIndex}`}>
          {formatted.slice(lastIndex, index)}
        </span>
      );
    }
    nodes.push(
      <span className={getTokenClassName(token)} key={`t-${index}`}>
        {token}
      </span>
    );
    lastIndex = index + token.length;
  }

  if (lastIndex < formatted.length) {
    nodes.push(
      <span className="text-text-muted" key={`p-${lastIndex}`}>
        {formatted.slice(lastIndex)}
      </span>
    );
  }

  return nodes;
}

export function ToolPayloadView({ content }: { readonly content: string }) {
  if (!content.trim()) {
    return null;
  }

  const formattedJson = tryFormatJson(content);
  if (formattedJson) {
    return (
      <pre className="bg-surface/70 border-border-subtle mt-1.5 overflow-x-auto rounded-md border px-2 py-1.5 font-mono text-[11px] leading-relaxed break-all whitespace-pre-wrap">
        {renderColorizedJson(formattedJson)}
      </pre>
    );
  }

  return (
    <div className="text-text-secondary mt-1 font-mono text-[11px] leading-relaxed break-all whitespace-pre-wrap">
      {content}
    </div>
  );
}
