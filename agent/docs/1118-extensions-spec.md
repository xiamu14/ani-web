# 渐进披露（Tools / Skills / MCP）与 MCP 规则预编译设计规范

本文定义在现有 `Router + Role Agents + PermissionGuardHook + ToolExecutionQueueHook` 架构之上，引入 **Tools、Skills 与 MCP 渐进披露** 以及 **`mcpServers` 增量权限预编译** 的扩展设计规范。

---

## 一、 设计目标与核心原则

1. **最小常驻上下文与 Prefix Cache 稳定**：启动与常规请求不把海量工具 Schema、全量 `SKILL.md` 正文或外部 MCP 工具定义塞入 System Prompt 前缀；通过索引 + 增量 Delta Part 按需披露。
2. **权限与队列全覆盖**：无论通过 `ToolSearch` 动态激活的工具还是外部 MCP 工具，执行前均受最高优先级 `PermissionGuardHook (0)` 与 `ToolExecutionQueueHook (20)` 保护。
3. **MCP 预编译代替运行期重复定级**：`mcpServers` 配置变更时，仅对新增或 Schema 漂移的工具调用一次 `Jev`（`typesafe/jev-1.14`）完成静态定级并落盘，运行期 `evaluate_tool_rule` 实现 O(1) 查表。

---

## 二、 三大维度的渐进披露方案

### 1. Tools 渐进披露（基于 `pydantic_ai.capabilities.ToolSearch`）
- **常驻层**：`SuperAgentRouter` 仅暴露 6 个核心编排工具；Role Agent 仅常驻高频基础工具（如 `read_file`、`edit_file`、`list_files`）。
- **按需激活层**：低频/扩展工具集配置 `defer_loading`，默认仅向模型暴露轻量检索入口；模型检索命中后，由 Pydantic AI 通过增量 `ToolAvailabilityDeltaPart` 动态注入对应工具 Schema，不破坏前序 KV Cache。

### 2. Skills 渐进披露（基于 `pydantic_ai_harness.skills.Skills`）
采用三级按需披露并严格遵守 `AGENTS.md` 目录边界（**仅挂载于执行层 Role Agents**，`SuperAgentRouter` 不挂载 Skills，保持 6 核心编排工具与主会话零 Skill 污染）：
- **三层作用域平等加载与统一渲染**：工作区（`<work_path>/.agent/skills`）、全局（`~/.agent/skills`）、内置（`src/agent/skills`）统一通过公开 API `pydantic_ai_harness.skills.Skills.load()` 解析、校验与安全扫描，封装为 `Capability(defer_loading=True)` + `CombinedCapability` 交付框架 `DeferredCapabilityLoader`。本地运行时三层统一保持 `in_run_workspace=True`（L2 均输出 `Skill directory`），不因是否调用 `set_workspace` 或来源作用域区别对待（未绑定工作区时仅省略工作区层）。
- **L1 目录索引（常驻）**：注入动态 `instructions`（仅含 `name + description` 及可选 `when_to_use` 极简清单，不含正文步骤）；切换 `work_path` 时在请求尾部增量更新并自动撤回旧工作区已加载 Skill，保持前序 Prefix Cache 稳定。
- **L2 指令正文（按需）**：任务匹配某 Skill 时，模型调用 `load_capability` 获取该 `SKILL.md` 完整步骤，**仅在 Role 子会话内按需加载**，不写入主会话或无关分支的 `context_entries` 历史。
- **L3 附属资源（按需）**：`scripts/`、`references/`、`examples/` 仅在执行对应步骤时按需读取，受统一权限规则与队列锁保护。
- **Skill 零特权与跨工作区权限边界**：
  - 任何作用域（含全局/内置）的 Skill 均不享有路径白名单，其后续工具调用无差别受 `PermissionGuardHook (0)` 与 `ToolExecutionQueueHook (20)` 约束：
    - 跨工作区写 → `forbid` 硬拒（需由 Router 通过 `dispatch_role_agent` 另起绑定目标工作区的任务执行）；
    - 跨工作区读（含全局/内置 Skill L3 附属资源）→ 经 `FORBIDDEN_SENSITIVE_PATH_PATTERNS` 防逃逸后统一走 `review.path.outside_workspace_read`（`Jev` + `UserApprovalPort` 单次 `run_id` 临时授权；Step 6.5 在 `PermissionGuardHook (0)` 保护下解除 `FileSystem` 单根围栏以打通审过放行）；
    - 跨工作区 `shell` → 命中 `review.cmd.outside_workspace_access`；未绑定工作区 → fail-closed；
    - `load_capability`（`tool_kind="capability-load"`）在 `ToolExecutionQueueHook` 中归为 `lock_mode="none"`，避免误抢工作区写锁。

### 3. MCP 渐进披露与懒连接（基于 `pydantic_ai.capabilities.MCP`）
- **配置边界**：支持合并「全局配置（`~/.agent/mcp.json`）+ 当前工作区自身配置（`<work_path>/.agent/mcp.json`）」，遵循 `AGENTS.md` 边界原则，不向上继承父目录。
- **多 Role 共享持有（多对多而非唯一绑定）**：
  - 同一 Tool 集或 MCP Server 可同时挂载给多个合适角色（如 `roles: ["researcher", "coder"]` 或 `["coder", "tester"]`），而非排他性 1:1 唯一绑定；
  - 若用户在某 Server 下显式声明 `"roles": [...]`，按指定角色集合挂载；
  - 若外部用户未指定 `roles`，在预编译阶段由 `Jev` 产出多选集合 `recommended_roles` 并落盘（纯只读/检索类可同时分派给 `researcher`、`reviewer`、`coder`；含写/变更/构建类仅分派给 `coder`、`tester`）。`Router` 不挂载任何重型 MCP。
- **按需懒连接与多 Role 并发隔离**：Role Worker 运行时按 `role.name in server.roles` 过滤出该角色持有的 `MCPToolset` 子集（工具较多时结合 `ToolSearch` 延迟披露 Schema），每次任务在独立 `async with agent:` 作用域内按需建立并释放连接，连接实例互不干扰、杜绝僵尸子进程。
- **同用户跨 Role 全工具共享协调（`ToolLockCoordinator`）**：连接隔离不能隔离底层共享状态（不仅是 Postgres/Playwright 等有状态 MCP，也包括本地重叠路径、命令句柄与会话控制工具）。`ToolExecutionQueueHook` 在 `open_runtime` 装配时统一注入同一个进程级 `ToolLockCoordinator` 覆盖**所有工具**（本地文件、`shell`、Router 工具与 MCP 工具）：无状态只读操作（`shared`）跨 Role 并发；所有**有状态或有副作用的工具（`exclusive`）**按各自资源键（`path:<...>`、`ws:<...>`、`cmd:<...>`、`conv:<...>`、`mcp_server:<endpoint_fingerprint>`）跨 Role 与 Router 全局协调串行。

---

## 三、 `mcpServers` 增量权限与队列预编译

```mermaid
flowchart LR
    Cfg["mcpServers 配置新增/修改"] --> Diff["计算 tool schema_hash<br/>(server + name + desc + inputSchema + annotations)"]
    Diff -- "指纹未变" --> Hit["复用已落盘 mcp_tool_policies"]
    Diff -- "新增或漂移 (增量)" --> Probe["短暂连接 Server 拉取 list_tools() 后立即断开"]
    Probe --> Jev["调用 Jev 一次性静态定级<br/>level(forbid/review/pass) + lock_mode(shared/exclusive)"]
    Jev --> Save["写入 SQLite mcp_tool_policies 表"]
    Save --> Runtime["运行期 evaluate_tool_rule / resolve_tool_resource O(1) 查表"]
```

1. **增量探测与断连**：仅对新增或配置变更的 `mcpServers` 建立短暂连接拉取 `list_tools()`，获取 `name`、`description`、`inputSchema` 与 `annotations`（如 `readOnlyHint` / `destructiveHint`）后立即关闭连接，保持运行时懒加载特性。
2. **`Jev` 一次性静态定级**：
   - **权限级别（`level`）**：
     - `forbid`：高危破坏/删库/凭据导出类 MCP 工具 → 运行期直接硬拒；
     - `review`：外部写操作/发消息/变更状态/敏感数据读取 → 运行期走 `PermissionReviewer`（结合 `user_intent` + `UserApprovalPort`）；
     - `pass`：纯只读无副作用检索（结合 `readOnlyHint=True` 且 `Jev` 确认无敏感风险）→ 运行期零 RTT 直接放行。
   - **队列锁模式与角色推荐**：同时输出 `lock_mode`（`shared` / `exclusive`，资源键 `mcp:<server_name>:<tool_name>`）与 `recommended_roles`（当用户配置未显式指定 `roles` 时生效）。
3. **Fail-Safe 兜底**：未在 `mcp_tool_policies` 登记或运行时 `schema_hash` 漂移的 MCP 工具，运行期一律兜底为 `REVIEW` + `exclusive`。

---

## 四、 Hook 优先级集成

通过 `src/agent/hooks/priority.py` 的 `sort_capabilities` 统一保持：
- `PermissionGuardHook (0)` > `POLICY (10)` > `ToolExecutionQueueHook (20)` > `ToolSearch / Skills / MCP (50)` > `StepPersistence (90)`。

---

## 五、 Skill 与 `mcpServer` 的去重与版本控制策略

采用 **“作用域覆盖优先为主 + 同级 SemVer 仲裁为辅 + 指纹绑定缓存版本”** 统一策略：

### 1. Skill 去重与版本选择
- **去重主键**：`SKILL.md` frontmatter 中的 `name`。
- **跨作用域覆盖**：优先级固定为 **当前工作区（`<work_path>/.agent/skills`） > 用户全局（`~/.agent/skills`） > 应用内置**；项目本地同名 Skill 直接覆盖全局，不用全局高版本逆转项目边界。
- **同作用域冲突（SemVer 仲裁）**：同作用域冲突发生在同作用域多目录场景（如工作区 `[".agent/skills", ".agents/skills"]` 或全局多目录）。支持 frontmatter 顶层 `version` 或 `metadata.version`（缺省 `"0.0.0"`，按 SemVer 2.0.0 比较）。同一作用域下出现同名 Skill 时优先保留 SemVer 较高版本；同版本则保留先扫描项并在 `skipped` 记录告警；损坏/无效 `SKILL.md` 先入 `skipped(reason="invalid")`，不遮蔽低版本有效项；被 `ArtifactScanner` 拦截的 `flagged` 项保留占位，防止低优先级副本顶替。

### 2. `mcpServer` 去重、版本锁定与策略失效
- **两层去重**：
  1. **名称去重（`server_name`）**：合并全局与工作区 `mcp.json` 时，**工作区同名 Server 整体覆盖全局条目**（整条替换，不跨源拼接 `command` 与 `args`）。
  2. **同源去重（`endpoint_fingerprint`）**：对 stdio（规范化 `command + args`）或 HTTP/SSE（规范化 `url`）计算端点指纹；若同一 Role 下存在不同名但同源的 Server，保留高优先级作用域（工作区级）或同级高版本，避免重复注入两份相同工具。
- **版本锁定与缓存联动**：
  - 尊重项目工作区显式锁定的包版本/配置版本；同作用域多来源声明同一服务（按 `serverInfo.name` 识别）时选较高版本。
  - `mcp_tool_policies` 缓存键强绑定 `(server_name, server_version, tool_name, schema_hash)`：一旦 `@latest` 等配置导致底层 `serverInfo.version` 或任一工具 `inputSchema` 变化，指纹立即失配并仅对该增量工具重跑 `Jev` 定级，防止旧策略误用到新版。
