# 渐进披露（Tools / Skills / MCP）与 MCP 预编译实施计划（Plan）

本文依据 [渐进披露扩展设计规范](./1118-extensions-spec.md)，按“小步快走、单步可独立测试验证”原则拆分为 5 个增量步骤（**Step 6.1 – Step 6.5**），严格保持单文件 `< 500` 行及权限/队列解耦边界。

---

## 一、 分步实施清单（TODO 状态）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 6.1** | `extensions/`、`context/` | 实现 `extensions/skills.py`（封装 `pydantic_ai_harness.skills.Skills`，支持工作区>全局>内置作用域去重、同级 SemVer 版本仲裁、L1 索引扫描与动态刷新） | `tests/test_skills_disclosure.py` | `[ ]` TODO |
| **Step 6.2** | `extensions/`、`core/` | 接入 `pydantic_ai.capabilities.ToolSearch` 延迟加载扩展工具，验证与 `sort_capabilities`、`PermissionGuardHook` 及 `ToolExecutionQueueHook` 联动 | `tests/test_tool_search.py` | `[ ]` TODO |
| **Step 6.3** | `db/`、`permissions/` | 新增 `mcp_tool_policies` 持久化表与 `permissions/mcp_compiler.py`（工作区>全局整条覆盖、同源 `endpoint_fingerprint` 去重、`(server_version, schema_hash)` 增量对比、`Jev` 预分类 `forbid/review/pass` + `lock_mode` + `recommended_roles`） | `tests/test_mcp_compiler.py` | `[ ]` TODO |
| **Step 6.4** | `permissions/`、`tools/` | 扩展 `rules.py` 与 `tool_queue.py` 支持预编译 MCP 规则 O(1) 查表、未登记/漂移兜底 `REVIEW`，并将 `ToolExecutionQueueHook` 升级为跨 Role + Router 共享全工具 `ToolLockCoordinator`（覆盖本地路径/`shell`/Router/有状态 MCP） | `tests/test_mcp_runtime_policy.py` | `[ ]` TODO |
| **Step 6.5** | `core/` | 在 `RoleSpec`、`WorkspaceLockManager` 与 `open_runtime` 装配 Skills / ToolSearch / 按 Role 过滤的 MCP 懒连接（`async with agent:` 生命周期清理） | 真实 LLM + 本地 Mock MCP E2E | `[ ]` TODO |

---

## 二、 各步可测试验证标准

* **Step 6.1（Skills 渐进披露）**：
  * 目录存在多个 `SKILL.md` 时，初始上下文仅包含 `name + description` 索引，不含正文步骤；
  * 切换工作区 `work_path` 时自动切换可见 Skill 清单，不继承上层无关目录规则，且不写入持久化历史。
* **Step 6.2（Tools 渐进披露）**：
  * 标记延迟加载的工具在未检索前不出现在首轮模型请求的可见工具定义中；
  * 经 `ToolSearch` 检索激活后可正常调用，且仍会触发 `PermissionGuardHook` 拦截与 `ToolExecutionQueueHook` 排队。
* **Step 6.3（MCP 增量预编译器）**：
  * 首次加载 2 个 MCP Server 时仅调用 1 次批量/逐个增量 `Jev` 分类并落盘 `mcp_tool_policies`；
  * 再次运行配置未变时 `Jev` 调用次数为 `0`（100% 命中缓存）；仅修改其中 1 个工具 Schema 时，只对该增量工具重跑 `Jev`。
* **Step 6.4（MCP 运行期 O(1) 策略与队列）**：
  * 命中预编译 `forbid` 的 MCP 工具零网络延迟直接拒绝；命中 `pass` 直接并发放行；命中 `review` 走 `PermissionReviewer`；
  * 未登记或 `schema_hash` 漂移的 MCP 工具自动兜底为 `REVIEW` + `exclusive`；同 Server 只读 MCP 工具 `shared` 并发、写工具 `exclusive` 串行。
* **Step 6.5（运行时装配与 E2E）**：
  * `open_runtime` 启动加载 `mcpServers` 完成增量预编译，Role Worker 按需懒连接 MCP 并在结束后干净释放连接（无残留子进程）；
  * 跑通真实 LLM 端到端测试（Skills 按需加载 + ToolSearch 激活 + MCP 权限拦截与执行）及 Logfire 链路验证。
