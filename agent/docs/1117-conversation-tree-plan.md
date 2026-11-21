# 核心调度与 conversation Tree 实施计划（TODO）

本文在 [Context Tree 架构设计](./1117-conversation-tree.md)、[Super-Agent 架构设计](./1117-super-agent.md) 与 [重启恢复扩展设计](./1117-super-agent2.md) 基础上，采用**强解耦、依赖倒置**原则，规划从 Context Tree 生命周期到 Router 协调及后台调度的分步实施路径。

---

## 一、 强解耦分层与依赖方向

```
   core/router.py (前台协调 + 事件回流)
       │         │              │
       ▼         ▼              ▼ (仅依赖 Protocol 接口，不直接耦合调度实现)
    hooks/   context/     tools/router_tools.py (WorkspacePort / TaskDispatchPort)
                 │                        ▲
                 ▼                        │ (Step 4.2 注入真实实现)
    db/ (ConversationStore / TicketStore) core/scheduler.py ──▶ core/roles.py
```

1. **`context/`**：只依赖 `db/` 与文件系统，负责树生命周期（`E_root`、`conv_init`、重启恢复、`handoff` 分叉、`SemanticBranchRouter`）与目录级 `AGENTS.md` 边界加载，不感知 Router、Role Agent 或目录锁。
2. **`hooks/`**：只定义请求级与运行前置钩子（`RouterContextHook`、`SemanticRoutingHook`、`TaskSteerHook`），与 `router.py` 和 `scheduler.py` 解耦。
3. **`tools/`**：只定义 `WorkspacePort` 与 `TaskDispatchPort` 抽象协议并封装 Router 工具集，不绑定具体数据库或并发调度器。
4. **`core/router.py`**：只负责主对话即时响应与后台事件回流，通过依赖注入消费 `ContextTreeManager`、`hooks/` 与 `TaskDispatchPort`。

---

## 二、 分步实施清单（TODO 状态）

| 步骤 | 目标模块 | 核心交付内容 | 状态 |
| :--- | :--- | :--- | :--- |
| **Step 0** | `db/` | SQLite Schema、`SQLModelConversationStore`、`SQLModelTicketStore` 及验收用例 | `[x]` 已完成 |
| **Step 1** | `context/` | `instructions.py`（目录边界 `AGENTS.md` 加载）+ `tree_manager.py`（`ContextTreeManager` 生命周期、绑定、分叉与恢复）+ 验收用例 | `[x]` 已完成 |
| **Step 2** | `tools/` | `router_tools.py`（`WorkspacePort` / `TaskDispatchPort` 协议 + `set_workspace` / `dispatch_role_agent` / `steer_or_revise_task` / `list_tasks`） | `[x]` 已完成 |
| **Step 3** | `core/` | `router.py`（`SuperAgentRouter` 前台即时应答与事件回推）+ 验收用例（`TestModel` + Fake `TaskDispatchPort`） | `[x]` 已完成 |
| **Step 4.1** | `core/` | `roles.py`（`RoleSpec`、`RoleRegistry`、`RoleSessionManager`）+ `tests/test_roles.py` | `[x]` 已完成 |
| **Step 4.2** | `core/` | `scheduler.py`（`WorkspaceLockManager` 异步目录锁与排队调度）+ `runtime.py` 装配真实 Role & 调度器 + `tests/test_scheduler.py` | `[x]` 已完成 |
| **Step 4.3** | `hooks/`, `db/`, `core/` | `tickets.depends_on` 持久化 + `hooks/steer_hook.py`（`TaskSteerHook`）+ `steer_or_revise`/`cancel_task` 完整实现 + 启动 `recover_main_conversation_tasks` 恢复联调 | `[x]` 已完成 |

---

## 三、 各步验收标准

* **Step 1（Context Tree 域）**：
  * 首次启动幂等创建 `E_root`；首条消息创建 `conv_init` 锚点（初始 `work_path=None`）。
  * `bind_workspace` 校验目录合法性、原地更新 `work_path`（与 `switch_conversation` 解耦，不新建/切换分支）；工作区 `AGENTS.md` 从持久化历史抽离，在请求末尾（最新 `UserPrompt` 之前）动态注入以兼顾上下文纯净与 Prefix Cache。
  * 支持 `switch_conversation`（仅切换已有分支，不新建）与 `list_conversations`；支持 `fork_handoff` 写入 `stop_backtrack=1` 交接节点隔离父历史。
* **Step 2（Router 工具契约）**：
  * `set_workspace` 对不存在目录抛出 `ModelRetry`；`steer_or_revise_task` 在 `user_confirmed=False` 时强制返回确认提示。
* **Step 3（Router 协调器）**：
  * Router 永不阻塞后台任务，能随时响应用户消息并在收到后台事件时注入或唤醒汇报。
* **Step 4.1（Role 蓝图与任务级上下文会话）**：
  * `RoleSpec` 与 `RoleRegistry` 保持无状态；`RoleSessionManager` 按 `(main_conversation_id, task_id)` 隔离加载与保存 `role_conversation_id`，支持切换主对话与 `ConversationConflict` 重试。
* **Step 4.2（真实 Role 装配、目录锁与异步任务联调）**：
  * `WorkspaceLockManager` 实现 `TaskDispatchPort` 并接入 `open_runtime` 与 `SuperAgentRouter`：支持在 Router 对话中唤起不同 Role 执行多步骤异步任务，异目录/无目录并发、同目录 FIFO 排队、`finally` 自动移交锁并回推完成事件。
* **Step 4.3（Steer / Revise、依赖阻塞语义与重启恢复）**：
  * 持久化 `tickets.depends_on`：上游 `failed` / `interrupted` 时下游保持 `queued`（不自动放行，也不在下游提供绕过入口），必须由用户处理上游（原链 Revise/重试成功，或显式取消上游后自动解除该依赖并放行下游）；上游 `failed` 不自动重试。
  * 实现 `TaskSteerHook` 与 `steer_or_revise` / `cancel_task`（运行中实时注入、排队中合并、终态/失败/中断原链续跑与兜底 `failed` 落盘），并接入 `open_runtime` 重启恢复 `recover_main_conversation_tasks` 重建排队与依赖图。
