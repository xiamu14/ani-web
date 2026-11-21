import { memo } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const REMARK_PLUGINS = [remarkGfm];

const MARKDOWN_COMPONENTS: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  h1: ({ children }) => (
    <h1 className="mt-3 mb-1.5 text-base font-semibold first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-2.5 mb-1.5 text-sm font-semibold first:mt-0">
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-2 mb-1 text-sm font-medium first:mt-0">{children}</h3>
  ),
  ul: ({ children }) => (
    <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="border-border text-text-secondary mb-2 border-l-2 pl-3 italic last:mb-0">
      {children}
    </blockquote>
  ),
  pre: ({ children }) => (
    <pre className="border-border-subtle bg-surface-subtle mb-2 overflow-x-auto rounded-lg border p-2.5 font-mono text-xs leading-relaxed last:mb-0">
      {children}
    </pre>
  ),
  code: ({ className, children }) => {
    const isBlock = Boolean(className?.includes("language-"));
    if (isBlock) {
      return <code className={className}>{children}</code>;
    }
    return (
      <code className="bg-surface-subtle rounded px-1 py-0.5 font-mono text-[12px]">
        {children}
      </code>
    );
  },
  a: ({ href, children }) => (
    <a
      className="text-accent underline underline-offset-2"
      href={href}
      rel="noreferrer"
      target="_blank"
    >
      {children}
    </a>
  ),
  table: ({ children }) => (
    <div className="mb-2 overflow-x-auto last:mb-0">
      <table className="border-border-subtle w-full border-collapse border text-xs">
        {children}
      </table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-border-subtle bg-surface-subtle border px-2 py-1 text-left font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border-border-subtle border px-2 py-1">{children}</td>
  ),
};

function MarkdownContentInner({ content }: { readonly content: string }) {
  return (
    <div className="text-sm leading-relaxed wrap-break-word">
      <ReactMarkdown
        components={MARKDOWN_COMPONENTS}
        remarkPlugins={REMARK_PLUGINS}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export const MarkdownContent = memo(MarkdownContentInner);
