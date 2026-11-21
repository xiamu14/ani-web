import { type } from "arktype";
import type { AttachmentKind } from "@/schemas/workspace.ts";
import { artifactFormatSchema, roleNameSchema } from "@/schemas/workspace.ts";

export const WORKBENCH_REF_MIME = "application/x-workbench-ref";

const fileDragPayloadSchema = type({
  kind: "'file'",
  path: "string",
  name: "string",
  isDir: "boolean",
  "sizeLabel?": "string",
});

const artifactDragPayloadSchema = type({
  kind: "'artifact'",
  artifactId: "string",
  artifactCode: "string",
  title: "string",
  format: artifactFormatSchema,
  sizeLabel: "string",
});

const taskDragPayloadSchema = type({
  kind: "'task'",
  taskId: "string",
  taskCode: "string",
  role: roleNameSchema,
  title: "string",
});

export const dragRefPayloadSchema = fileDragPayloadSchema
  .or(artifactDragPayloadSchema)
  .or(taskDragPayloadSchema);
export type DragRefPayload = typeof dragRefPayloadSchema.infer;

export function writeDragRef(
  dataTransfer: DataTransfer,
  payload: DragRefPayload
): void {
  dataTransfer.effectAllowed = "copy";
  dataTransfer.setData(WORKBENCH_REF_MIME, JSON.stringify(payload));
  let fallback = "";
  if (payload.kind === "file") {
    fallback = `@file:${payload.path}`;
  } else if (payload.kind === "artifact") {
    fallback = `[[artifact:${payload.artifactCode}]]`;
  } else {
    fallback = `@${payload.role}(${payload.taskCode})`;
  }
  dataTransfer.setData("text/plain", fallback);
}

export function hasInternalDragRef(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) {
    return false;
  }
  return Array.from(dataTransfer.types).includes(WORKBENCH_REF_MIME);
}

export function readDragRef(
  dataTransfer: DataTransfer | null
): DragRefPayload | null {
  if (!dataTransfer) {
    return null;
  }
  const raw = dataTransfer.getData(WORKBENCH_REF_MIME);
  if (!raw) {
    return null;
  }
  try {
    const json: unknown = JSON.parse(raw);
    const parsed = dragRefPayloadSchema(json);
    return parsed instanceof type.errors ? null : parsed;
  } catch {
    return null;
  }
}

const IMAGE_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".svg",
  ".avif",
]);
const AUDIO_EXTENSIONS = new Set([".mp3", ".wav", ".ogg", ".m4a", ".flac"]);
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".webm", ".mkv"]);
const DOCUMENT_EXTENSIONS = new Set([
  ".pdf",
  ".md",
  ".txt",
  ".json",
  ".jsonc",
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".py",
  ".csv",
  ".yaml",
  ".yml",
]);
const DOCUMENT_MIME_TYPES = new Set([
  "application/pdf",
  "application/json",
  "application/markdown",
  "application/xml",
]);

function extractExtension(fileName: string): string {
  const dot = fileName.lastIndexOf(".");
  return dot >= 0 ? fileName.slice(dot).toLowerCase() : "";
}

export function inferAttachmentKind(
  mediaType: string,
  fileName: string
): AttachmentKind {
  const lowered = mediaType.toLowerCase().trim();
  if (lowered.startsWith("image/")) {
    return "image";
  }
  if (lowered.startsWith("audio/")) {
    return "audio";
  }
  if (lowered.startsWith("video/")) {
    return "video";
  }
  if (lowered.startsWith("text/") || DOCUMENT_MIME_TYPES.has(lowered)) {
    return "document";
  }

  const ext = extractExtension(fileName);
  if (IMAGE_EXTENSIONS.has(ext)) {
    return "image";
  }
  if (AUDIO_EXTENSIONS.has(ext)) {
    return "audio";
  }
  if (VIDEO_EXTENSIONS.has(ext)) {
    return "video";
  }
  if (DOCUMENT_EXTENSIONS.has(ext)) {
    return "document";
  }
  return "binary";
}
