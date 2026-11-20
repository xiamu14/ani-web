# 外接通信协议（GUI Protocol）、交互中心与上下文计量实施计划（Plan）

- **基线阶段**：Step 11.1–11.4 完成后（Attachment/Artifact & OPT-3/4/6 存储层已落盘）
- **对应设计规范**：[1120-gui-protocol-spec.md](./1120-gui-protocol-spec.md)
- **已确认核心决策**：
  1. **一机一实例**：本机单实例锁与发现（`~/.agent/runtime.lock` + `~/.agent/runtime.json` `0600`），同时监听 UDS（`~/.agent/runtime.sock`, `trusted_local=True`）与 Localhost HTTP（`127.0.0.1:<port>` + Bearer Token + Origin/Host 校验），支持 `--parent-pid` Sidecar 守护。
  2. **逐条勾选权限审核**：保留 `PermissionReviewer` 15ms 批聚合，`ApprovalItem` 增加唯一 `item_id`，`ApprovalDecision` 新增 `item_decisions` 逐条裁决映射（未列条目 `fail-closed` 拒绝，支持单条 `grant_category_for_run` 与向后兼容）。
  3. **Router & Role Agents 全量挂载 `AskUser`**：直接复用 `pydantic_ai_harness.ask_user.AskUser`，结合 `InteractionBroker` 支持结构化选择建议卡片，同时明确区分阻塞式 `AskUser` 与非阻塞 `suggested_actions`。
- **实施原则**：小步快走、强解耦、单文件 `< 500` 行、每一步均独立验收（`unittest` + `basedpyright` + `ruff`），保持对现有 `run()` / `ask()` 与已有测试 100% 向后兼容。

---

## 一、 分步实施 TODO 清单（Step 12.1 – Step 12.7）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 12.1** | `protocol/models_base.py`、`protocol/models.py`、`protocol/events_schema.py`、`protocol/event_bus.py`、`protocol/schema_export.py`、`protocol/__init__.py` | **Layer 0 协议契约、跨语言 Schema 导出与持久 SSE 事件总线（零 HTTP 依赖）**：定义全部 Rust `serde`/TS 友好的 Pydantic v2 请求/响应/视图模型与 `ErrorEnvelope`；定义 8 大域判别联合 `ProtocolEvent` 信封；实现 `RuntimeEventBus`（异步 pub/sub、容量 5000 环形缓冲、`Last-Event-ID` 重放、`stream.resync_required`、20ms/16字符 Delta 微批合帧、按 `task_ids` 动态流过滤）；实现 `openapi.json` / `events.schema.json` 导出工具 | `tests/test_protocol_bus.py` (模型往返序列化、RingBuffer 重放/过期 `resync_required`、Delta 合帧与 `task_stream` 订阅过滤单测) | `[x]` DONE |
| **Step 12.2** | `protocol/interactions.py`、`permissions/reviewer.py`、`hooks/permission_hook.py` | **统一交互中心 `InteractionBroker` 与逐条勾选审批升级**：升级 `ApprovalItem`（`item_id`）、`ApprovalContext` 与 `ApprovalDecision`（`item_decisions` 逐条裁决 + `fail-closed` + 单条 `grant_category_for_run` + 向后兼容）；升级 `PermissionGuardHook` 透传 `ApprovalContext`；实现 `InteractionBroker`（统一承载逐条 Approval、`pydantic_ai_harness.ask_user.AskUser` `Answerer`、`McpElicitationHandler`、并发/幂等响应、`cancel_for_scope` 与 `AskUserAnswer.note` 附件转句柄规避 `UploadedFile` 限制） | `tests/test_protocol_interactions.py` (15ms 批内逐条通过/拒绝/部分授权、未覆盖条目 fail-closed、AskUser 问答含附件句柄转换、MCP Elicitation 与 Scope 取消防挂死单测) | `[x]` DONE |
| **Step 12.3** | `llm/rate_limiter.py`、`hooks/priority.py`、`hooks/router_hooks.py`、`core/router.py`、`core/scheduler.py`、`protocol/stream_bridge.py`、`protocol/task_tracker.py` | **Core 流式化、优雅中断、插话纠偏、`AskUser` 全量挂载与多模态附件入模**：补齐 `CooldownRetryModel.request_stream()` 首包前 429 冷却退避重试（吐出首 chunk 后禁止重试）；新增 `HookPriority.ACCOUNTING = 85` 与 `RouterSteerHook`；`SuperAgentRouter` 支持 `event_stream_handler`、`CancellationToken`、`delivery_mode`（`auto`/`steer`/`queue`/`interrupt_and_send`）、多模态附件入模、`AskUser` 与 `extract_and_verify_citations()`；`WorkspaceLockManager` 为每个 Role Worker 装配独立 `CancellationToken`、`AskUser`、`live_phase` 进度与结构化 `TaskEvent` 回调 | `tests/test_streaming_and_controls.py` (`request_stream` 首包前 429 重试/首包后不重试、Router 流式推流+中断保留断点+Steer 插话、Role 优雅取消、AskUser 暂停/恢复与结构化任务进度单测) | `[x]` DONE |
| **Step 12.4** | `protocol/accounting.py`、`protocol/token_buckets.py`、`llm/jev_client.py`、`db/models.py`、`db/schema.py`、`db/tree_queries.py` | **Context Window 9 桶精准计量、三层 Token 账单与 Conversation Tree 双层读模型**：实现 `ContextAccountingHook`（`before_model_request` 利用 `tiktoken` + 离线估算兜底 + LRU 缓存分桶估算 `ModelRequestContext` 9 类占用与延迟加载/OPT-6 省流指标 → `after_model_request` 按真实 `RequestUsage.input_tokens` 校准 `overhead`，严格遵循 `UsageBase.input_tokens` 包含式口径防重复累加 cache tokens）；`JevHttpClient` 增加 `on_usage` 回调单独统计 `auxiliary_jev_usage`；`schema.py` / `models.py` / `tree_queries.py` 增量增加 `context_snapshots`、`conversation_aux_usage`、整树拓扑只读查询 `read_tree_topology()` 与 `context_entries` 轻量分页 | `tests/test_context_accounting_and_tree.py` (9 桶精确分类、`sum(buckets) == actual_input_tokens` 校准、cache_read 不重复累加断言、Jev 辅助消耗分离、双层 Tree 分页与 `stop_backtrack` 截断验证) | `[x]` DONE |
| **Step 12.5** | `server/auth.py`、`server/lifecycle.py`、`server/state.py`、`server/app.py`、`server/routes_*.py`、`server/cli.py`、`core/runtime.py`、`pyproject.toml` | **Layer 2 Starlette HTTP/SSE Server 与一机一实例守护进程**：实现 `SingleInstanceLock` (`flock`)、`runtime.json` (`0600`) 原子写、UDS (`0600`, `trusted_local=True`) + Localhost HTTP (`Bearer` + `Host`/`Origin` 校验)、`--parent-pid` Sidecar 守护与 `SIGTERM` 优雅清理；实现全套 `/v1` REST 与 SSE 路由（含 `multipart` 上传、UDS 本地直注、HTTP `Range` + `ETag` 预览、Tree Fork/Handoff/Switch、Task Steer/Cancel、Interaction 响应、Context/Usage 查询）；更新 `open_runtime()` 与 `agent serve` 入口 | `tests/test_protocol_server.py` (单实例互斥锁与残余 socket 清理、Bearer/Origin 鉴权拦截、非 UDS 调用 `/attachments/local` 返回 403、HTTP `Range` 切片读取与全部 REST 路由契约测试) | `[x]` DONE |
| **Step 12.6** | `tests/test_protocol_e2e.py`、`docs/schemas/` | **全链路协议 E2E 集成验收与 Schema 契约产物导出**：通过 ASGI `httpx.AsyncClient`（含 SSE 流消费者）端到端验证：首屏快照 → 发送多模态消息与附件 → 流式 delta → 触发 `AskUser` 选择回答 → 派发多 Role Task → 逐条勾选权限审批（同批一条放行一条附言拒绝）→ 中途插话 Steer 与中断恢复 → Tree Fork/Handoff → Context 9 桶校准与三层用量核对；导出并校验 `docs/schemas/openapi.json` 与 `docs/schemas/events.schema.json`（含 `serde` tag 完整性检查） | `tests/test_protocol_e2e.py` (全链路协议集成 E2E 与 JSON Schema 导出完整性校验) | `[x]` DONE |
| **Step 12.7** | 全量 `src/`、`tests/` 与 `docs/1120-gui-protocol-plan.md` | **全量回归、文件行数守卫与代码质量门禁**：核查所有新建与修改的 `src/agent/**/*.py` 均 `< 500` 行；执行 `basedpyright src/ tests/` (`0 errors`) 与 `ruff check src/ tests/` (`All checks passed!`)；运行全仓现有 + 新增 `unittest` 套件零回归，回写本计划状态为 DONE | 全量 `unittest` + `basedpyright` + `ruff` + 行数审计脚本 | `[x]` DONE |

---

## 二、 各步可测试验收标准

1. **Step 12.1（协议模型与持久 SSE 事件总线）**：
   - 所有 `ProtocolEvent` 变体序列化后必含 `protocol_version="1.0"`、单调递增 `seq`、UTC RFC3339 `ts`、显式 `type` 与对应强类型 `payload`，且不含未知字段或 `serde` 无法判别的裸联合类型；
   - `RuntimeEventBus` 在缓冲区容量 `N=5` 时发布 8 条事件：传入 `Last-Event-ID: 5` 精准重放 `6, 7, 8`；传入 `Last-Event-ID: 1` 精准推送 `stream.resync_required`；
   - 连续发送 10 条同一 `(conversation_id, run_id, part_index)` 的文本 delta（间隔 `< 5ms`），经 20ms 合帧窗合并为 `<= 2` 条 SSE 帧；遇 `message.part.end` 立即冲刷；
   - 未订阅 `task_ids=["T2"]` 的客户端只收到 `@T2` 的 `task.status_changed` / `task.progress`，不收到 `T2` 的 `message.part.delta`；调用订阅更新后立即开始接收。

2. **Step 12.2（`InteractionBroker` 与逐条勾选审批）**：
   - **逐条勾选**：模拟同批 15ms 聚合收到 `item_1(shell)`、`item_2(edit_file)`、`item_3(mcp_tool)`，前端提交 `item_decisions={item_1: approved+grant_category, item_2: denied(reason="先别改配置文件")}`（故意漏掉 `item_3`）：
     - `item_1` 放行并写入 `run_grants`（同 `run_id` 下再次调用同 category 免弹窗）；
     - `item_2` 拒绝并返回 `"先别改配置文件"` 给 `PermissionGuardHook` 触发 `ModelRetry`；
     - `item_3` 因未列入映射被 **fail-closed 拒绝**；
   - **向后兼容**：旧调用方仅传 `ApprovalDecision(approved=True, grant_categories_for_run=("shell",))` 时行为与原版完全一致；
   - **`AskUser` 桥接**：`AskUser` 发出包含 2 个选项的问题，GUI 提交选中项 + `attachment_ids=["att_xxx"]`，`InteractionBroker` 将附件转为 `[[attachment:att_xxx | ...]]` 写入 `AskUserAnswer.note`，通过 Pydantic 校验而不触发 `UploadedFile` 异常；
   - **冲突与取消**：同一 `interaction_id` 并发提交两次响应，第二次返回 `409 INTERACTION_ALREADY_RESOLVED`；调用 `cancel_for_scope(task_id="T1")` 立即解除挂起 Future 并广播 `interaction.cancelled`。

3. **Step 12.3（Core 流式化、中断、Steer 与 `AskUser` 全量挂载）**：
   - **`request_stream` 429 重试边界**：模拟首包前连续 2 次 429 后第 3 次成功建流，自动冷却退避并完整吐出流；模拟已吐出第 1 个文本 chunk 后连接中断，**绝不触发二次重试**，防止输出重复拼接；
   - **Router 中断与插话**：流式生成半途触发 `CancellationToken.cancel()`，Router 优雅终止并调用 `repair_messages()` 将已生成消息持久化；`delivery_mode="steer"` 在工具循环间隙将用户新建议注入下一轮 `before_model_request`；
   - **全量挂载**：验证 `SuperAgentRouter` 与 Role Worker (`coder`/`researcher` 等) 均内置 `ask_user_question` 工具；Role 等待用户回答期间 `live_phase == "waiting_user"`，且不阻塞同目录读锁或其他无依赖工作区任务。

4. **Step 12.4（Context Window 9 桶计量与 Conversation Tree 读模型）**：
   - 构造同时包含全局 `AGENTS.md`、局部 `AGENTS.md`、L1/L2 Skills、MCP 工具、L0/L1/L2 Memory、图片/文档附件与历史消息的请求：
     - `ContextAccountingHook` 准确将各部分归入对应 9 桶，不交叉重复计费；
     - `after_model_request` 收到真实 `RequestUsage(input_tokens=10000, cache_read_tokens=6000)` 后，断言 **`window_used_tokens == 10000`**（而非 `16000`）、`cache_hit_ratio == 0.60`、`sum(9 buckets) == 10000`；
   - Jev 语义路由与权限复核产生的用量单独记录在 `conversation_aux_usage` 与 `auxiliary_jev_usage`，不污染主会话上下文窗口水位；
   - `read_tree_topology()` 正确返回根分支、Handoff/Full-Fork 派生分支与 Role 子会话；Entry 分页正确识别 `stop_backtrack=1` 截断且对窗口外大附件返回 URL 占位而非内联 Base64。

5. **Step 12.5（HTTP/SSE Server 与一机一实例进程管理）**：
   - **一机一实例互斥**：第 1 个实例持有 `runtime.lock` 并写入 `runtime.json` (`0600`) 与 `runtime.sock` (`0600`)；第 2 个实例尝试启动同一目录时立即报错退出，不破坏已有 `runtime.json` 与 SQLite；模拟前次进程死后遗留陈旧 `runtime.sock`（`flock` 已释放）时新实例可安全清理并接管启动；
   - **传输安全隔离**：HTTP 请求缺失/错误 Bearer Token 返回 `401`；携带非白名单 `Origin` 返回 `403`；通过 HTTP（非 UDS）调用 `POST /v1/attachments/local` 强制返回 `403 UNTRUSTED_LOCAL_ENDPOINT`，而通过 UDS 调用成功零拷贝注册；
   - **Range 预览**：`GET /v1/attachments/{id}/content` 带 `Range: bytes=0-99` 返回 `206 Partial Content` 与精准 100 字节片段。

6. **Step 12.6（全链路 E2E 与 Schema 产物校验）**：
   - 单条 E2E 测试打通完整生命周期，零 `asyncio` 未关闭任务告警；
   - 导出的 `docs/schemas/openapi.json` 与 `docs/schemas/events.schema.json` 为合法 JSON Schema Draft 2020-12 / OpenAPI 3.1，所有事件均具备显式 `type` 与 `payload` 契约，可直接被 `openapi-typescript` / `typify` 消费。

7. **Step 12.7（全量门禁）**：
   - `src/agent/` 下每个新增与改动 Python 文件严格 `< 500` 行；
   - `basedpyright src/ tests/` 报告 `0 errors, 0 warnings`；
   - `ruff check src/ tests/` 报告 `All checks passed!`；
   - 原有全部单元测试与 Step 12.1–12.6 新增测试 100% 通过。

---

## 三、 关键工程风险与防呆检查清单

- [ ] **单文件行数上限（`< 500` 行）**：按职责拆分 `protocol/`（models / events_schema / event_bus / interactions / accounting）与 `server/`（auth / lifecycle / app / 5 个 routes 模块 / cli），禁止堆出上千行巨型 `app.py` 或 `models.py`。
- [ ] **`UsageBase.input_tokens` 包含式口径红线**：绝不将 `cache_read_tokens` 或 `cache_write_tokens` 再次加到 `input_tokens` 上，严格遵循 `pydantic_ai` 2.69.0 口径并写明单测断言。
- [ ] **`AskUserAnswer.note` 禁止 `UploadedFile` 约束**：用户回答建议时附带的 `attachment_ids` 必须转为 `[[attachment:att_xxx | ...]]` 文本句柄放入 `note`，严禁构造 `UploadedFile` 实例塞入 `AskUserAnswer.note`。
- [ ] **逐条审批 Fail-Closed**：`item_decisions` 缺失条目一律拒绝；单条 `grant_category_for_run=True` 仅在同条目 `approved=True` 时生效，且仅绑定到当前 `run_id`。
- [ ] **`request_stream` 首包后禁重试**：仅在进入 `wrapped.request_stream()` 前或读取首事件前的建连阶段拦截 429 重试；流已产生任何 delta 后发生异常必须向上传播，禁止静默重放造成文本重复。
- [ ] **UDS 权限与陈旧文件安全清理**：监听前通过 `os.umask(0o077)` + `chmod 0o600` 保护 `runtime.sock` 与 `runtime.json`；`flock` 失败时不误删在运行实例的文件；`--parent-pid` 退出或收到 `SIGTERM` 时优雅触发 `CancellationToken` 落盘后退出。
- [ ] **单向依赖与零回归**：`core/`、`hooks/`、`permissions/`、`db/`、`llm/` 绝不引入 Web 框架依赖；所有现有 CLI 与单元测试（`ask()` / `run()` / `open_runtime()` 无参调用）保持完全兼容。
