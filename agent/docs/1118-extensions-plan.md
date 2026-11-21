# 渐进披露（Tools / Skills / MCP）与 MCP 预编译实施计划（Plan）

本文依据 [渐进披露扩展设计规范](./1118-extensions-spec.md)，按“小步快走、单步可独立测试验证”原则拆分为 5 个增量步骤（**Step 6.1 – Step 6.5**），严格保持单文件 `< 500` 行及权限/队列解耦边界。

---

## 一、 分步实施清单（TODO 状态）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 6.1** | `extensions/`、`context/`、`permissions/`、`tools/` | 实现 `extensions/skill_resolver.py` + `extensions/skills.py`（仅面向 Role Agents，三层平等 `Skills.load()` + 统一 `in_run_workspace=True` + 工作区>全局>内置作用域去重 + 同级 SemVer 仲裁 + `ctx.workspace` 动态刷新）、`context/instructions.py` 目录边界辅助、`tools/tool_queue.py` 放行 `load_capability` (`none`)、`permissions/rules.py` 补齐跨工作区 `shell` `REVIEW` 与无工作区 fail-closed（零白名单） | `tests/test_skills_disclosure.py` | `[x]` DONE |
| **Step 6.2** | `extensions/`、`core/` | 接入 `pydantic_ai.capabilities.ToolSearch` 延迟加载扩展工具，验证与 `sort_capabilities`、`PermissionGuardHook` 及 `ToolExecutionQueueHook` 联动 | `tests/test_tool_search.py` | `[x]` DONE |
| **Step 6.3** | `pyproject.toml`、`extensions/`、`db/`、`permissions/` | 显式补齐 `pydantic-ai-slim[mcp]` 依赖；实现 `extensions/mcp_loader.py` 与 `extensions/mcp_auth.py`（两级 `mcp.json` 合并、`${VAR}` 展开、脱敏 `endpoint_fingerprint`；**严格解耦外部 OAuth 授权与运行期只读消费**：运行期/探测期 `oauth` 从 `FileTreeStore` 提取已落盘 `access_token` 映射为纯静态 `BearerAuth`，绝不向 Role Agent 挂载 `OAuthClientProvider` 状态机；仅外部扩展授权接口 `interactive=True` 挂载 `_InteractiveOAuth`）；新增 `mcp_tool_policies` 持久化表与 `permissions/mcp_compiler.py`（同源去重、无真实 `access_token` 非阻塞返回 `auth_required`、`(server_version, schema_hash)` 增量对比、`Jev` 预分类 `forbid/review/pass` + `lock_mode` + `recommended_roles`） | `tests/test_mcp_compiler.py` | `[x]` DONE |
| **Step 6.4** | `permissions/`、`tools/` | 扩展 `rules.py` 与 `tool_queue.py` 支持预编译 MCP 规则 O(1) 查表、未登记/漂移兜底 `REVIEW`，并将 `ToolExecutionQueueHook` 升级为跨 Role + Router 共享全工具 `ToolLockCoordinator`（覆盖本地路径/`shell`/Router/有状态 MCP） | `tests/test_mcp_runtime_policy.py` | `[x]` DONE |
| **Step 6.5** | `core/`、`server/`、`agent/__init__.py` | 在 `RoleSpec`、`WorkspaceLockManager` 与 `open_runtime` 仅向 Role Agents 装配 Skills（不挂 Router）、ToolSearch 与按 Role 过滤的已授权就绪 MCP 懒连接（`async with agent:` 生命周期清理，Role Agent 仅使用纯静态 `BearerAuth` 且不参与 OAuth 流程）；通过外部扩展接口 `POST /v1/mcps/{name}/auth` 及 CLI `agent mcp auth <name>` 独立触发 OAuth 授权并刷新工作区预编译缓存 | `tests/test_mcp_runtime_wiring.py` | `[x]` DONE |

---

## 二、 各步可测试验证标准

* **Step 6.1（Skills 渐进披露）**：
  * 目录存在多个 `SKILL.md` 时，初始上下文仅包含 `name + description`（及 `when_to_use`）索引，不含正文步骤，且前序 Prefix Cache 稳定；
  * 工作区、全局与内置三层作用域平等加载与渲染（本地运行时均保持 `in_run_workspace=True` 输出 `Skill directory`，不因是否绑定工作区区别对待）；同级多目录按 SemVer 2.0.0 仲裁，高版本胜出且无效文件不遮蔽有效项；
  * 切换工作区 `work_path` 时自动刷新可见 Skill 清单并撤回旧工作区已加载 Skill，不继承上层无关目录，且不写入主会话或无关分支历史；
  * 跨工作区零特权校验（Skill 不享路径白名单：跨工作区写 `forbid`、读与 `shell` 均触发 `review`，未绑定工作区 fail-closed），且 `load_capability` 在 `ToolExecutionQueueHook` 下按 `none` 无锁并发，不阻塞工作区写锁。
* **Step 6.2（Tools 渐进披露）**：
  * 标记延迟加载的工具在未检索前不出现在首轮模型请求的可见工具定义中；
  * 经 `ToolSearch` 检索激活后可正常调用，且仍会触发 `PermissionGuardHook` 拦截与 `ToolExecutionQueueHook` 排队。
* **Step 6.3（MCP Loader / Auth 与增量预编译器）**：
  * `mcp_loader.py` 正确合并全局与工作区 `mcp.json`（工作区整条覆盖、`${VAR}` 展开、跨工作区 Token 隔离），Token 轮换/刷新不改变 `endpoint_fingerprint`；
  * `LocalOAuthReadinessChecker` 严格区分真实 OAuth Token（含非空 `access_token`）与 `FileTreeStore` 集合头元数据（`*-info.json`）或 DCR `client_info`；未完成授权的 `oauth` Server 返回 `auth_required`、`blocked=True`，且不阻塞启动；
  * 运行期（`interactive=False`）对已就绪的 `oauth` Server 提取已落盘 `access_token` 构造纯静态 `BearerAuth`，绝不向 Role Agent 挂载 `OAuthClientProvider`；
  * 再次运行配置未变时 `Jev` 调用次数为 `0`（100% 命中缓存）；仅修改其中 1 个工具 Schema 时，只对该增量工具重跑 `Jev`。
* **Step 6.4（MCP 运行期 O(1) 策略与队列）**：
  * 命中预编译 `forbid` 的 MCP 工具零网络延迟直接拒绝；命中 `pass` 直接并发放行；命中 `review` 走 `PermissionReviewer`；
  * 未登记或 `schema_hash` 漂移的 MCP 工具自动兜底为 `REVIEW` + `exclusive`；同 Server 只读 MCP 工具 `shared` 并发、写工具 `exclusive` 串行。
* **Step 6.5（运行时装配、外部扩展 OAuth 授权与 E2E）**：
  * `open_runtime` 启动加载 `mcpServers` 完成增量预编译，Role Worker 仅挂载已授权就绪的 MCP（静态 `BearerAuth`）并在结束后干净释放连接（无残留子进程、零 OAuth 状态机参与）；
  * 外部扩展授权接口（`POST /v1/mcps/{name}/auth` 与 CLI `agent mcp auth <name>`）使用 `_InteractiveOAuth`（跳过后端 `httpx2` 预检直拉系统浏览器、支持匿名 `list_tools` Server 强制触发 PRM 全 Scope 授权），落盘校验通过后刷新工作区预编译快照；
  * 跑通真实 LLM 端到端测试（Skills 按需加载 + ToolSearch 激活 + Stdio/Bearer/Mock OAuth MCP 权限拦截与执行）及 Logfire 链路验证。
