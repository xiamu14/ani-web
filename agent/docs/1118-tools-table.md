# 现有工具清单：状态、副作用与并发/串行队列策略

本文梳理当前系统中 **Router 工具集**（`src/agent/tools/router_tools.py`）与 **Role Agent Harness 能力集**（`Coder`、`FileSystem`、`Shell`）的全部工具，按**状态（有/无）**与**副作用（有/无）**定义 `ToolExecutionQueueHook` 的并发与串行调度策略。

---

## 一、 工具分级总表

> **判定原则**：
> - **无状态 + 无副作用**的纯查询工具：`none`（无锁直接并发）。
> - **无状态 + 无副作用**但依赖文件/目录内容一致性的只读工具：`shared`（同资源多读可并发，与写操作互斥防脏读）。
> - **有状态变更 或 有写盘/外部命令副作用**的工具：`exclusive`（同资源/父子嵌套路径/同工作区严格串行，防写饥饿）。

| 来源模块 | 工具名称 | 状态（有/无） | 副作用（有/无） | 队列策略（`lock_mode`） | 资源键（`resource_key`） | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Harness `FileSystem` / `Coder`** | `read_file` | 无状态 | 无副作用 | **`shared`** | `path:<norm_file_path>` | 同文件多读并发，与写互斥 |
| **Harness `FileSystem` / `Coder`** | `file_info` | 无状态 | 无副作用 | **`shared`** | `path:<norm_file_path>` | 读取元信息 |
| **Harness `FileSystem` / `Coder`** | `list_directory`、`list_files` | 无状态 | 无副作用 | **`shared`** | `path:<norm_dir_path>` | 目录枚举 |
| **Harness `FileSystem` / `Coder`** | `search_files`、`find_files`、`grep` | 无状态 | 无副作用 | **`shared`** | `path:<norm_search_root>` | 全文/文件名检索 |
| **Harness `FileSystem` / `Coder`** | `write_file` | 有文件变更 | **有写盘副作用** | **`exclusive`** | `path:<norm_file_path>` | 支持 `paths_overlap` 父子目录冲突检测 |
| **Harness `FileSystem` / `Coder`** | `edit_file` | 有文件变更 | **有写盘副作用** | **`exclusive`** | `path:<norm_file_path>` | 同文件/父子目录写互斥 |
| **Harness `FileSystem`** | `create_directory` | 有目录变更 | **有建目录副作用** | **`exclusive`** | `path:<norm_dir_path>` | 父子目录互斥 |
| **Harness `Coder` / `Shell`** | `shell`、`run_command`、`start_command` | 有进程/工作区状态 | **有命令与写盘副作用** | **`exclusive`** | `ws:<workspace_root>` | 同工作区独占串行，防止命令与文件写踩踏 |
| **Harness `Shell`** | `check_command`、`stop_command` | 有后台进程状态 | `stop` 有状态变更 | **`exclusive`** | `cmd:<command_id>` | 按进程句柄串行 |
| **Router 工具集** | `list_tasks`、`inspect_workspace_locks` | 无状态 | 无副作用 | **`none`** | — | 只读内存/DB 快照查询，无锁并发 |
| **Router 工具集** | `set_workspace` | 有会话状态（`work_path`） | 有状态变更 | **`exclusive`** | `conv:<main_conversation_id>` | 防止单轮并行调用同时改写会话工作区 |
| **Router 工具集** | `dispatch_role_agent` | 有账本状态（`tickets`） | 有派发副作用 | **`exclusive`** | `conv:<main_conversation_id>` | 配合调度器 `_dispatch_lock` 串行分配 `@task_id` |
| **Router 工具集** | `steer_or_revise_task` | 有任务状态 | 有控制副作用 | **`exclusive`** | `ticket:<main_conv_id>:<task_id>` | 同任务控制指令串行 |
| **Router 工具集** | `cancel_task` | 有任务状态 | 有控制副作用 | **`exclusive`** | `ticket:<main_conv_id>:<task_id>` | 同任务取消与状态清理串行 |
| **Pydantic AI 框架元工具** | `load_capability`、`search_tools`（`tool_kind` 为 `capability-load` / `tool-search`） | 无状态 | 无副作用 | **`none`** | — | 按需披露检索与指令加载，不抢占工作区写锁 |

---

## 二、 资源键冲突匹配规则

1. **`path:<norm_path>` 类别**：复用 `paths_overlap(path_a, path_b)`（`Path.is_relative_to` 双向检查），覆盖文件与父目录之间的嵌套冲突（如 `write_file("/repo/a.py")` 与 `list_directory("/repo")` 互斥）。
2. **`ws:<workspace_root>` 类别**：`shell` / `run_command` / `start_command` 视为覆盖整个 `<workspace_root>`，与该工作区内的所有 `path:<...>` 读写工具互斥。
3. **`mcp_server:<endpoint_fingerprint>` 类别**：有状态或有副作用的 MCP 工具按外部服务端点指纹全局独占串行；无状态只读 MCP 工具按 `shared` 并发。
4. **`conv:` / `ticket:` / `cmd:` 类别**：按精确资源键做读写互斥，跨会话、跨任务、跨进程句柄互不阻塞。
5. **跨 Role 全局协调作用域**：在 `open_runtime` 运行时，Router 与全部 Role Worker 的 `ToolExecutionQueueHook` 共享同一个进程级 `ToolLockCoordinator`，确保上述所有工具（本地文件、`shell`、Router 控制与 MCP）的读写锁在跨 Role 并发时同样生效。
6. **未识别扩展工具兜底**：未知工具默认保守按 `exclusive` + `ws:<workspace_root>`（或 `tool:<tool_name>`）串行执行，避免未知副作用并发。
