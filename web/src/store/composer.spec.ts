import { beforeEach, describe, expect, it, vi } from "vitest";
import { inferAttachmentKind } from "@/lib/drag-ref.ts";
import type { GuiProtocolClient } from "@/protocol/client.ts";
import { setProtocolClient } from "@/protocol/runtime-client.ts";
import {
  addArtifactReference,
  addDragPayloadReference,
  addFileAttachments,
  addWorkspaceFileReference,
  clearComposerReferences,
  composerStore,
  removeComposerReference,
  resetComposerStore,
} from "./composer.ts";
import {
  resetWorkspaceStore,
  sendMessage,
  workspaceStore,
} from "./workspace.ts";

describe("输入框附件与拖放引用状态", () => {
  beforeEach(() => {
    setProtocolClient(null);
    resetComposerStore();
    resetWorkspaceStore();
  });

  it("能根据 MIME 类型与扩展名推导附件类别", () => {
    expect(inferAttachmentKind("image/png", "shot.png")).toBe("image");
    expect(inferAttachmentKind("application/pdf", "spec.pdf")).toBe("document");
    expect(inferAttachmentKind("", "notes.md")).toBe("document");
    expect(inferAttachmentKind("audio/mpeg", "clip.mp3")).toBe("audio");
    expect(inferAttachmentKind("video/mp4", "demo.mp4")).toBe("video");
    expect(inferAttachmentKind("", "archive.bin")).toBe("binary");
  });

  it("添加本地附件文件并自动去重", () => {
    const first = new File(["hello"], "design.png", { type: "image/png" });
    const dup = new File(["hello"], "design.png", { type: "image/png" });
    const second = new File(["{}"], "config.json", {
      type: "application/json",
    });

    addFileAttachments([first, dup, second]);
    const refs = composerStore.state.references;
    expect(refs).toHaveLength(2);
    expect(refs[0]).toMatchObject({
      kind: "attachment",
      attachmentKind: "image",
      title: "design.png",
    });
    expect(refs[1]).toMatchObject({
      kind: "attachment",
      attachmentKind: "document",
      title: "config.json",
    });
  });

  it("支持文件树引用、产物引用与拖拽载荷去重和删除", () => {
    addWorkspaceFileReference({
      path: "src/styles.css",
      name: "styles.css",
      isDir: false,
      sizeLabel: "2.2 KB",
    });
    addDragPayloadReference({
      kind: "file",
      path: "src/styles.css",
      name: "styles.css",
      isDir: false,
      sizeLabel: "2.2 KB",
    });
    addArtifactReference({
      id: "art-1",
      artifactCode: "art_layout",
      title: "三栏布局方案",
      format: "markdown",
      sizeLabel: "3.2 KB",
    });
    addDragPayloadReference({
      kind: "artifact",
      artifactId: "art-1",
      artifactCode: "art_layout",
      title: "三栏布局方案",
      format: "markdown",
      sizeLabel: "3.2 KB",
    });

    const refs = composerStore.state.references;
    expect(refs).toHaveLength(2);
    const firstId = refs[0]?.id;
    expect(firstId).toBeDefined();
    if (firstId) {
      removeComposerReference(firstId);
    }
    expect(composerStore.state.references).toHaveLength(1);
    clearComposerReferences();
    expect(composerStore.state.references).toHaveLength(0);
  });

  it("连接后发送纯引用消息时会将附件和引用写入消息记录", () => {
    const sendSpy = vi.fn(async () => ({
      client_message_id: "msg-1",
      delivery_mode: "started",
      resolved_conversation_id: "route-main",
      routing_action: "CONTINUE",
      run_id: "run-1",
    }));
    setProtocolClient({
      sendMessage: sendSpy,
    } as unknown as GuiProtocolClient);
    workspaceStore.setState((state) => ({
      ...state,
      activeRouteId: "route-main",
    }));

    addDragPayloadReference({
      kind: "artifact",
      artifactId: "art-2",
      artifactCode: "art_schema",
      title: "工作台数据契约",
      format: "json",
      sizeLabel: "4.6 KB",
    });

    const beforeCount =
      workspaceStore.state.messagesByThread["route-main"]?.length ?? 0;
    sendMessage("", composerStore.state.references);

    const messages = workspaceStore.state.messagesByThread["route-main"] ?? [];
    expect(messages).toHaveLength(beforeCount + 1);
    const latest = messages.at(-1);
    expect(latest?.references).toHaveLength(1);
    expect(latest?.references?.[0]).toMatchObject({
      kind: "artifact",
      artifactCode: "art_schema",
    });
    expect(sendSpy).toHaveBeenCalledTimes(1);
    setProtocolClient(null);
  });
});
