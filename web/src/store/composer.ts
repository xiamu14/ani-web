import { createStore } from "@tanstack/react-store";
import { formatFileSize } from "@ui-sdk/react/file-input";
import { nanoid } from "nanoid";
import type { DragRefPayload } from "@/lib/drag-ref.ts";
import { inferAttachmentKind } from "@/lib/drag-ref.ts";
import type { ArtifactItem, ComposerReference } from "@/schemas/workspace.ts";

export interface DraggingRefPreview {
  readonly kind: "file" | "artifact" | "task";
  readonly label: string;
  readonly payload: DragRefPayload;
}

export interface ComposerState {
  readonly draggingRef: DraggingRefPreview | null;
  readonly references: readonly ComposerReference[];
}

function createInitialComposerState(): ComposerState {
  return {
    draggingRef: null,
    references: [],
  };
}

export const composerStore = createStore<ComposerState>(
  createInitialComposerState()
);

const stagedFilesById = new Map<string, File>();

export function resetComposerStore(): void {
  stagedFilesById.clear();
  composerStore.setState(() => createInitialComposerState());
}

export function setDraggingRef(draggingRef: DraggingRefPreview | null): void {
  composerStore.setState((state) => ({ ...state, draggingRef }));
}

export function addFileAttachments(files: readonly File[]): void {
  if (files.length === 0) {
    return;
  }
  composerStore.setState((state) => {
    const next = [...state.references];
    for (const file of files) {
      const sizeLabel = formatFileSize(file.size);
      const duplicate = next.some(
        (item) =>
          item.kind === "attachment" &&
          item.title === file.name &&
          item.sizeLabel === sizeLabel
      );
      if (duplicate) {
        continue;
      }
      const mediaType = file.type || "application/octet-stream";
      const id = `att-${nanoid(8)}`;
      stagedFilesById.set(id, file);
      next.push({
        id,
        kind: "attachment",
        attachmentKind: inferAttachmentKind(mediaType, file.name),
        title: file.name,
        mediaType,
        sizeLabel,
      });
    }
    return { ...state, references: next };
  });
}

export function getStagedFiles(
  references: readonly ComposerReference[]
): File[] {
  const files: File[] = [];
  for (const ref of references) {
    if (ref.kind !== "attachment") {
      continue;
    }
    const file = stagedFilesById.get(ref.id);
    if (file) {
      files.push(file);
    }
  }
  return files;
}

export function addWorkspaceFileReference(file: {
  readonly path: string;
  readonly name: string;
  readonly isDir: boolean;
  readonly sizeLabel?: string;
}): void {
  composerStore.setState((state) => {
    const exists = state.references.some(
      (item) => item.kind === "file" && item.path === file.path
    );
    if (exists) {
      return state;
    }
    return {
      ...state,
      references: [
        ...state.references,
        {
          id: `file-${nanoid(8)}`,
          kind: "file",
          path: file.path,
          name: file.name,
          isDir: file.isDir,
          ...(file.sizeLabel ? { sizeLabel: file.sizeLabel } : {}),
        },
      ],
    };
  });
}

export function addArtifactReference(
  artifact: Pick<
    ArtifactItem,
    "id" | "artifactCode" | "title" | "format" | "sizeLabel"
  >
): void {
  composerStore.setState((state) => {
    const exists = state.references.some(
      (item) => item.kind === "artifact" && item.artifactId === artifact.id
    );
    if (exists) {
      return state;
    }
    return {
      ...state,
      references: [
        ...state.references,
        {
          id: `artref-${nanoid(8)}`,
          kind: "artifact",
          artifactId: artifact.id,
          artifactCode: artifact.artifactCode,
          title: artifact.title,
          format: artifact.format,
          sizeLabel: artifact.sizeLabel,
        },
      ],
    };
  });
}

export function addTaskReference(task: {
  readonly taskId: string;
  readonly taskCode: string;
  readonly role: "coder" | "reviewer" | "tester" | "researcher";
  readonly title: string;
}): void {
  composerStore.setState((state) => {
    const exists = state.references.some(
      (item) => item.kind === "task" && item.taskId === task.taskId
    );
    if (exists) {
      return state;
    }
    return {
      ...state,
      references: [
        ...state.references,
        {
          id: `taskref-${nanoid(8)}`,
          kind: "task",
          taskId: task.taskId,
          taskCode: task.taskCode,
          role: task.role,
          title: task.title,
        },
      ],
    };
  });
}

export function addDragPayloadReference(payload: DragRefPayload): void {
  if (payload.kind === "file") {
    addWorkspaceFileReference(payload);
  } else if (payload.kind === "artifact") {
    addArtifactReference({
      id: payload.artifactId,
      artifactCode: payload.artifactCode,
      title: payload.title,
      format: payload.format,
      sizeLabel: payload.sizeLabel,
    });
  } else {
    addTaskReference({
      taskId: payload.taskId,
      taskCode: payload.taskCode,
      role: payload.role,
      title: payload.title,
    });
  }
}

export function removeComposerReference(id: string): void {
  stagedFilesById.delete(id);
  composerStore.setState((state) => ({
    ...state,
    references: state.references.filter((item) => item.id !== id),
  }));
}

export function clearComposerReferences(): void {
  stagedFilesById.clear();
  composerStore.setState((state) =>
    state.references.length === 0 ? state : { ...state, references: [] }
  );
}
