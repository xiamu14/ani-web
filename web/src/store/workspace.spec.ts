import { beforeEach, describe, expect, it } from "vitest";
import {
  createMockRoute,
  resetWorkspaceStore,
  returnToRoute,
  selectRoute,
  selectTask,
  sendMessage,
  setDashboardTab,
  workspaceStore,
} from "./workspace.ts";

describe("工作台状态管理", () => {
  beforeEach(() => {
    resetWorkspaceStore();
  });

  it("初始状态为空白（无离线演示数据）", () => {
    expect(workspaceStore.state.routes).toEqual([]);
    expect(workspaceStore.state.tasks).toEqual([]);
    expect(workspaceStore.state.treeNodes).toEqual([]);
    expect(workspaceStore.state.activeRouteId).toBe("");
  });

  it("未连接时发送消息与新建会话不会写入本地假数据", () => {
    sendMessage("离线测试");
    createMockRoute();

    expect(workspaceStore.state.routes).toEqual([]);
    expect(workspaceStore.state.messagesByThread).toEqual({});
  });

  it("支持切换已加载的角色任务与路由", () => {
    workspaceStore.setState((state) => ({
      ...state,
      activeRouteId: "route-main",
      routes: [
        {
          id: "route-main",
          title: "主线",
          workPath: "/repo",
          status: "active",
          headEntryId: "ent-1",
          revision: "root",
          updatedAt: "刚刚",
          messageCount: 0,
        },
        {
          id: "route-gpi",
          title: "协议",
          workPath: "/repo",
          status: "idle",
          headEntryId: "ent-2",
          revision: "full_fork",
          updatedAt: "刚刚",
          messageCount: 0,
        },
      ],
      tasks: [
        {
          id: "task-1",
          routeId: "route-main",
          taskCode: "@T1",
          role: "coder",
          title: "实现接入",
          status: "running",
          lockMode: "exclusive",
          dependsOn: [],
          workPath: "/repo",
          updatedAt: "刚刚",
        },
      ],
      treeNodes: [
        {
          id: "tree-route-gpi",
          label: "协议",
          kind: "route",
          refId: "route-gpi",
          entryId: "ent-2",
          status: "idle",
          tokens: 0,
          isHead: false,
        },
      ],
    }));

    selectTask("task-1");
    expect(workspaceStore.state.activeTaskId).toBe("task-1");
    expect(workspaceStore.state.activeRouteId).toBe("route-main");

    returnToRoute();
    expect(workspaceStore.state.activeTaskId).toBeNull();

    selectRoute("route-gpi");
    expect(workspaceStore.state.activeRouteId).toBe("route-gpi");
    expect(workspaceStore.state.selectedTreeNodeId).toBe("tree-route-gpi");
  });

  it("支持切换右侧标签页", () => {
    setDashboardTab("files");
    expect(workspaceStore.state.dashboardTab).toBe("files");
  });
});
