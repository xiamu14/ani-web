import { type } from "arktype";
import { describe, expect, it } from "vitest";
import { initialWorkspaceSnapshot } from "@/data/mock-workspace.ts";
import {
  chatMessageSchema,
  workspaceSnapshotSchema,
} from "@/schemas/workspace.ts";

describe("工作台数据结构校验", () => {
  it("内置静态快照符合工作台 Schema", () => {
    const parsed = workspaceSnapshotSchema(initialWorkspaceSnapshot);
    expect(parsed).not.toBeInstanceOf(type.errors);
  });

  it("非法消息发送方会被 ArkType 拦截", () => {
    const invalid = chatMessageSchema({
      id: "msg-bad",
      threadId: "route-main",
      sender: "unknown-role",
      senderLabel: "未知",
      content: "测试",
      createdAt: "19:50",
    });
    expect(invalid).toBeInstanceOf(type.errors);
  });
});
