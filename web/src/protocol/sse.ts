import type { ProtocolEvent } from "./types.ts";

export interface SseConnectionOptions {
  readonly afterSeq?: number;
  readonly authToken: string;
  readonly baseUrl: string;
  readonly onDisconnect?: (error: Error | null) => void;
  readonly onEvent: (event: ProtocolEvent) => void;
  readonly onReconnect?: () => void;
}

const FRAME_SPLIT_REGEX = /\r?\n\r?\n/u;
const LINE_SPLIT_REGEX = /\r?\n/u;
const TRAILING_SLASH_REGEX = /\/+$/u;
const MAX_BACKOFF_MS = 10_000;

function parseSseFrame(rawFrame: string): ProtocolEvent | null {
  const lines = rawFrame.split(LINE_SPLIT_REGEX);
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (dataLines.length === 0) {
    return null;
  }
  const payloadText = dataLines.join("\n");
  try {
    return JSON.parse(payloadText) as ProtocolEvent;
  } catch {
    return null;
  }
}

function buildSseHeaders(authToken: string, lastSeq: number): HeadersInit {
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
  };
  const trimmedToken = authToken.trim();
  if (trimmedToken) {
    headers.Authorization = `Bearer ${trimmedToken}`;
  }
  if (lastSeq > 0) {
    headers["Last-Event-ID"] = String(lastSeq);
  }
  return headers;
}

async function streamSseBody(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onChunk: (chunk: string) => void
): Promise<void> {
  const decoder = new TextDecoder();
  const writable = new WritableStream<Uint8Array>({
    write(chunk) {
      onChunk(decoder.decode(chunk, { stream: true }));
    },
  });
  await body.pipeTo(writable, { signal });
}

export function openProtocolSse(options: SseConnectionOptions): () => void {
  const controller = new AbortController();
  const base = options.baseUrl.trim().replace(TRAILING_SLASH_REGEX, "");
  let lastSeq = options.afterSeq ?? 0;
  let retryDelayMs = 1000;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let hasConnectedOnce = false;

  function scheduleReconnect() {
    if (controller.signal.aborted) {
      return;
    }
    const delay = retryDelayMs;
    retryDelayMs = Math.min(retryDelayMs * 2, MAX_BACKOFF_MS);
    reconnectTimer = setTimeout(() => {
      if (!controller.signal.aborted) {
        void pump();
      }
    }, delay);
  }

  async function pump() {
    const query = new URLSearchParams();
    if (lastSeq > 0) {
      query.set("after_seq", String(lastSeq));
    }
    const qs = query.toString();
    const url = `${base}/v1/events${qs ? `?${qs}` : ""}`;

    try {
      const response = await fetch(url, {
        method: "GET",
        headers: buildSseHeaders(options.authToken, lastSeq),
        signal: controller.signal,
      });
      if (!(response.ok && response.body)) {
        throw new Error(`SSE 连接失败 (${response.status})`);
      }

      if (hasConnectedOnce) {
        options.onReconnect?.();
      }
      hasConnectedOnce = true;
      retryDelayMs = 1000;

      let buffer = "";

      await streamSseBody(response.body, controller.signal, (chunk) => {
        buffer += chunk;
        const frames = buffer.split(FRAME_SPLIT_REGEX);
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const parsed = parseSseFrame(frame);
          if (parsed) {
            if (parsed.seq > lastSeq) {
              lastSeq = parsed.seq;
            }
            options.onEvent(parsed);
          }
        }
      });

      if (!controller.signal.aborted) {
        options.onDisconnect?.(null);
        scheduleReconnect();
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        options.onDisconnect?.(
          error instanceof Error ? error : new Error(String(error))
        );
        scheduleReconnect();
      }
    }
  }

  void pump();

  return () => {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    controller.abort();
  };
}
