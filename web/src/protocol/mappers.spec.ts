import { describe, expect, it } from "vitest";
import {
  mapBranchToRoute,
  mapContextSnapshot,
  mapEntryToChatMessage,
  mapTaskViewToRoleTask,
  mapTreeToNodes,
  mapTurnToChatMessages,
} from "./mappers.ts";
import type {
  BranchView,
  ContextWindowSnapshotView,
  EntryView,
  TaskView,
  TreeView,
} from "./types.ts";

describe("protocol mappers", () => {
  it("maps BranchView and TaskView into UI models", () => {
    const branch: BranchView = {
      active_task_count: 1,
      branch_mode: "root",
      conversation_id: "conv-1",
      head_entry_id: "ent-9",
      is_running: true,
      message_count: 4,
      pending_interaction_count: 0,
      scope: "main",
      status: "active",
      target_title: "协议联调",
      updated_at: "2026-11-20T12:00:00Z",
      usage_summary: {
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        input_tokens: 1000,
        output_reasoning_tokens: 0,
        output_tokens: 200,
        requests: 2,
        tool_calls: 1,
        total_tokens: 1200,
      },
      work_path: "/repo/web",
    };

    const route = mapBranchToRoute(branch, new Set(["conv-1"]), "conv-1");
    expect(route.id).toBe("conv-1");
    expect(route.status).toBe("active");
    expect(route.title).toBe("协议联调");

    const task: TaskView = {
      active_tool: {
        args_preview: "src/app.ts",
        started_at: "2026-11-20T12:00:00Z",
        tool_call_id: "tc-1",
        tool_name: "read_file",
      },
      blocked_by: [],
      created_at: "2026-11-20T12:00:00Z",
      current_step: 2,
      depends_on: [],
      live_phase: "calling_tool",
      lock_mode: "shared",
      main_conversation_id: "conv-1",
      mandate: "检查协议字段",
      max_requests: 10,
      max_tool_calls: 20,
      pending_interaction_ids: [],
      queue_position: 0,
      recent_steps: [],
      role_conversation_id: "conv-role-1",
      role_name: "reviewer",
      soft_limit_triggered: false,
      status: "running",
      task_id: "T1",
      tool_calls_count: 1,
      updated_at: "2026-11-20T12:00:00Z",
      usage: branch.usage_summary,
      work_path: "/repo/web",
    };

    const mappedTask = mapTaskViewToRoleTask(task, [
      {
        exclusive_holder: "T1",
        queued_tasks: [],
        shared_holders: [],
        work_path: "/repo/web",
      },
    ]);
    expect(mappedTask.id).toBe("conv-1:T1");
    expect(mappedTask.taskCode).toBe("@T1");
    expect(mappedTask.lockMode).toBe("exclusive");
    expect(mappedTask.activeToolName).toBe("read_file");

    const tree: TreeView = {
      active_conversation_id: "conv-1",
      branches: [branch],
      root_entry_id: "ent-0",
    };
    const nodes = mapTreeToNodes(
      tree,
      [mappedTask],
      new Set(["conv-1"]),
      "conv-1"
    );
    expect(nodes).toHaveLength(1);
    expect(nodes[0]?.children?.[0]?.kind).toBe("task");
  });

  it("maps 9-bucket ContextWindowSnapshotView and EntryView", () => {
    const snap: ContextWindowSnapshotView = {
      cache_hit_ratio: 0.4,
      calibrated: true,
      categories: [
        { category: "system_prompt", ratio: 0.1, sub_items: [], tokens: 1000 },
        {
          category: "environment_info",
          ratio: 0.05,
          sub_items: [],
          tokens: 500,
        },
        { category: "skills", ratio: 0.05, sub_items: [], tokens: 500 },
        { category: "mcp", ratio: 0.1, sub_items: [], tokens: 1000 },
        { category: "builtin_tools", ratio: 0.1, sub_items: [], tokens: 1000 },
        { category: "memory", ratio: 0.05, sub_items: [], tokens: 500 },
        { category: "attachments", ratio: 0.05, sub_items: [], tokens: 500 },
        { category: "messages", ratio: 0.4, sub_items: [], tokens: 4000 },
        { category: "overhead", ratio: 0.1, sub_items: [], tokens: 1000 },
      ],
      conversation_id: "conv-1",
      estimation_delta: 0,
      limit_known: true,
      model_name: "gemini-2.5-pro",
      run_id: "run-1",
      savings: {
        deferred_mcp_tokens_saved_est: 300,
        deferred_mcp_tools_count: 2,
        deferred_skills_count: 1,
        deferred_skills_tokens_saved_est: 200,
        opt6_omitted_media_count: 1,
      },
      tokenizer_name: "cl100k",
      ts: "2026-11-20T12:00:00Z",
      uncached_input_tokens: 6000,
      utilization_ratio: 0.05,
      window_limit_tokens: 200_000,
      window_used_tokens: 10_000,
    };

    const mappedContext = mapContextSnapshot(snap, "协议联调");
    expect(mappedContext.usedTokens).toBe(10_000);
    expect(mappedContext.segments[0]?.tokens).toBe(2000);
    expect(mappedContext.segments[1]?.tokens).toBe(5500);
    expect(mappedContext.segments[2]?.tokens).toBe(2000);
    expect(mappedContext.segments[3]?.tokens).toBe(500);

    const entry: EntryView = {
      attached_task_ids: [],
      child_branch_ids: [],
      created_at: "2026-11-20T12:00:00Z",
      entry_id: "ent-1",
      kind: "message",
      parts: [
        { part_kind: "thinking", role: "assistant", text: "先检查接口" },
        { part_kind: "text", role: "assistant", text: "已完成检查" },
      ],
      run_id: "run-1",
      stop_backtrack: false,
    };
    const msg = mapEntryToChatMessage(entry, "conv-1");
    expect(msg.thinking).toBe("先检查接口");
    expect(msg.content).toBe("已完成检查");
    expect(msg.sender).toBe("router");

    const reportMsgs = mapTurnToChatMessages(
      {
        attached_task_ids: [],
        child_branch_ids: [],
        created_at: "2026-11-21T09:00:00Z",
        run_id: "run-report",
        steps: [],
        user_entry_id: "ent-rep-1",
        user_text:
          '后台任务有新进展，请向用户简洁播报结果：\n<ticket_report kind="completed">\n@T1 (@researcher) 已完成:',
      },
      "conv-1",
      0
    );
    expect(reportMsgs[0]?.sender).toBe("role");
    expect(reportMsgs[0]?.role).toBe("researcher");
  });
});
