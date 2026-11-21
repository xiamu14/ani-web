# 1121 Unified Exception Handling Implementation Plan

## Step 1: 原生错误上报 Capability (`src/agent/hooks/error_hook.py` & `src/agent/hooks/__init__.py`)
- [x] 实现 `format_exception_detail(exc: BaseException) -> str`：处理空消息异常（如 `httpx.ConnectError('')` -> `"ConnectError"`）并自动提取 `__cause__` / `__context__`。
- [x] 实现 `RunErrorReportingHook(AbstractCapability[object])`：
  - 声明 `hook_priority = int(HookPriority.OBSERVABILITY)`；
  - 支持从 `ctx.deps.main_conversation_id` 动态解析 Router 会话 ID；
  - 覆写 Pydantic AI 原生 `on_tool_execute_error`、`on_model_request_error`、`on_run_error`。
- [x] 通过 `compile_capabilities(...)` 在 `SuperAgentRouter` 与 `WorkspaceLockManager._run_worker` 中统一挂载。

## Step 2: Role Worker 与 Router 运行边界统一收敛 (`src/agent/core/scheduler.py`, `src/agent/core/router.py`, `src/agent/core/lock_queue.py`)
- [x] 将 `scheduler.py` 的 `_run_worker` 异常捕获从窄元组扩展为 `except Exception as exc:`，将 `format_exception_detail(exc)` 写入 `ticket.result_summary`、发射 `emit_worker_outcome(..., outcome="failed")` 并通知主会话。
- [x] 移除 `src/agent/core/lock_queue.py` 中重复且不再使用的遗留函数 `format_worker_error`。
- [x] 在 `router.py` 的 `handle_user_message` 与 `_run_proactive` 中统一使用 `format_exception_detail(exc)`，并在 `handle_user_message` 发射 `run.failed` 后标记 `_run_failed_emitted = True`。

## Step 3: HTTP Server 后台任务与全局 500 兜底 (`src/agent/server/routes_chat.py`, `src/agent/server/app.py`)
- [x] 在 `routes_chat.py` 的 `_bg_run()` 中捕获 `Exception`：若 `_run_failed_emitted` 已标记则不再重复发射，否则记录日志并兜底发射 `run.failed`。
- [x] 在 `app.py` 中注册 `Exception -> _handle_internal_error`，返回 `500` 标准 `ErrorEnvelope`。

## Step 4: 单元测试与全量门禁验证 (`tests/test_error_handling.py`)
- [x] 新增 `tests/test_error_handling.py`，验证：
  1. `format_exception_detail` 对空消息异常（`ConnectError('')`）及带 `__cause__` 异常的格式化；
  2. `RunErrorReportingHook` 在工具抛错与 Run 抛错时的触发与动态 `main_conversation_id` 解析；
  3. Role Worker 工具抛出非 `AgentRunError`（如 `RuntimeError("")` / `ConnectError("")`）时，工单状态为 `failed`、`result_summary` 为真实错误摘要（绝不出现 `"Worker terminated unexpectedly."`），且正确发出 `run.failed` 事件；
  4. Router 异常通过 HTTP `/v1/conversations/{cid}/messages` 触发时，`run.failed` 仅发射一次（无重复发射）。
- [x] 执行行数检查（`< 500` 行）、`basedpyright`、`ruff check` 与全量 `unittest`。
