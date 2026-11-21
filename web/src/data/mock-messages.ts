import type { ChatMessage } from "@/schemas/workspace.ts";

export const mockMessagesByThread: Record<string, ChatMessage[]> = {
  "route-main": [
    {
      id: "msg-1",
      threadId: "route-main",
      sender: "user",
      senderLabel: "你",
      content: "在 web 目录初始化前端项目，先完成三栏静态工作台界面。",
      createdAt: "19:42",
      references: [
        {
          id: "ref-init-1",
          kind: "file",
          path: "AGENTS.md",
          name: "AGENTS.md",
          isDir: false,
          sizeLabel: "2.8 KB",
        },
      ],
    },
    {
      id: "msg-2",
      threadId: "route-main",
      sender: "router",
      senderLabel: "主路由",
      content:
        "已绑定工作区并梳理协议结构，正在按顺序执行脚手架搭建、代码审查与单元测试。",
      createdAt: "19:42",
      dispatchedTaskIds: ["task-1", "task-2", "task-3", "task-4"],
      artifactIds: ["art-1", "art-2"],
      toolTraces: [
        {
          id: "trace-1",
          name: "set_workspace",
          argsSummary: "ani-web/web",
          status: "done",
          durationMs: 42,
        },
        {
          id: "trace-2",
          name: "dispatch_role_agent",
          argsSummary: "@T4 researcher",
          status: "done",
          durationMs: 18,
        },
        {
          id: "trace-3",
          name: "dispatch_role_agent",
          argsSummary: "@T1 coder (exclusive)",
          status: "running",
          durationMs: 120,
        },
      ],
    },
    {
      id: "msg-3",
      threadId: "route-main",
      sender: "system",
      senderLabel: "系统通知",
      content: "@T4 调研已完成，前端组件将先使用静态数据展示完整结构。",
      createdAt: "19:45",
    },
  ],
  "route-gpi": [
    {
      id: "msg-gpi-1",
      threadId: "route-gpi",
      sender: "user",
      senderLabel: "你",
      content: "核对实时事件流与上下文树快照格式，确保字段命名一致。",
      createdAt: "18:20",
    },
    {
      id: "msg-gpi-2",
      threadId: "route-gpi",
      sender: "router",
      senderLabel: "主路由",
      content: "已分配调研任务梳理事件字段，当前会话处于空闲等待状态。",
      createdAt: "18:21",
      dispatchedTaskIds: ["task-5"],
      artifactIds: ["art-3"],
    },
  ],
  "route-policy": [
    {
      id: "msg-policy-1",
      threadId: "route-policy",
      sender: "user",
      senderLabel: "你",
      content: "检查工具调用的读写锁与审批等级默认值。",
      createdAt: "17:05",
    },
    {
      id: "msg-policy-2",
      threadId: "route-policy",
      sender: "router",
      senderLabel: "主路由",
      content: "只读角色默认共享读锁，写操作角色自动互斥排队，等待下一步指令。",
      createdAt: "17:06",
      artifactIds: ["art-4"],
    },
  ],
  "task-1": [
    {
      id: "msg-t1-1",
      threadId: "task-1",
      sender: "role",
      senderLabel: "编码 · @T1",
      role: "coder",
      content:
        "正在创建三栏工作台组件，右侧面板包含对话树、上下文与工作区标签页。",
      createdAt: "19:43",
      toolTraces: [
        {
          id: "trace-t1-1",
          name: "read_file",
          argsSummary: "postiler/web/package.json",
          status: "done",
          durationMs: 28,
        },
        {
          id: "trace-t1-2",
          name: "write_file",
          argsSummary: "src/components/layout/workbench-shell.tsx",
          status: "running",
          durationMs: 95,
        },
      ],
    },
  ],
  "task-2": [
    {
      id: "msg-t2-1",
      threadId: "task-2",
      sender: "system",
      senderLabel: "调度队列",
      content: "等待前置任务 @T1 释放目录写锁后自动开始只读代码审查。",
      createdAt: "19:43",
    },
  ],
  "task-3": [
    {
      id: "msg-t3-1",
      threadId: "task-3",
      sender: "system",
      senderLabel: "调度队列",
      content: "等待前置任务 @T1 完成后运行单元测试与类型检查。",
      createdAt: "19:43",
    },
  ],
  "task-4": [
    {
      id: "msg-t4-1",
      threadId: "task-4",
      sender: "role",
      senderLabel: "调研 · @T4",
      role: "researcher",
      content:
        "已完成前端接口梳理，确认使用树形分支、Token 分段统计和目录锁定状态。",
      createdAt: "19:44",
      artifactIds: ["art-1"],
      toolTraces: [
        {
          id: "trace-t4-1",
          name: "read_file",
          argsSummary: "agent/src/agent/core/runtime.py",
          status: "done",
          durationMs: 36,
        },
      ],
    },
  ],
  "task-5": [
    {
      id: "msg-t5-1",
      threadId: "task-5",
      sender: "role",
      senderLabel: "调研 · @T1",
      role: "researcher",
      content: "事件流支持任务派发、上下文快照更新与文件变更追踪。",
      createdAt: "18:22",
    },
  ],
};
