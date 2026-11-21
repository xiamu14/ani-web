# 1121 Unified Exception Handling & Reporting Specification

## 1. 背景与根因分析

在真实运行场景（如 `@T3` Role Worker 调用 `knowra` MCP 工具触发本地 OAuth 回调超时或连接失败，抛出 `httpx.ConnectError('')`）中，原有异常处理链路暴露出三类缺陷：

1. **Worker 异常捕获范围过窄**：
   - `WorkspaceLockManager._run_worker`（`src/agent/core/scheduler.py`）原先仅捕获 `(AgentRunError, ModelAPIError, UsageLimitExceeded, TimeoutError)`。
   - 当 MCP 工具、内置工具或网络层直接抛出 `httpx.HTTPError`（非 `ModelAPIError` 子类）、`OSError`、`RuntimeError` 等异常时，会穿透 `except` 分支直接落入 `finally: if not status_persisted:`：
     - 工单 `result_summary` 被硬编码覆盖为 `"Worker terminated unexpectedly."`，真实异常信息被彻底掩盖；
     - 仅发出 `ticket.status_changed`，**漏发 `run.failed`** SSE 事件；
     - 后台 `asyncio.Task` 产生 `Task exception was never retrieved`。
2. **空消息异常格式化丢失类型与上下文**：
   - 诸如 `httpx.ConnectError('')`、`TimeoutError()` 的 `str(exc)` 为空字符串 `""`；
   - 原有 `lock_queue.format_worker_error` 生成 `"ConnectError: "` 且未被全局复用。
3. **缺少基于 Pydantic AI 原生生命周期的统一错误可观测层与 HTTP 边界兜底**：
   - 工具级、模型请求级与 Run 级错误未通过 `AbstractCapability` 的原生错误 Hook 统一采集；
   - `routes_chat.py` 的后台任务 `_bg_run()` 若在 `self._agent.run` 之前抛出异常会导致 `asyncio.Task` 静默失败；若在 `self._agent.run` 内部抛出异常，又需避免 `router.py` 与 `routes_chat.py` 重复发送两次 `run.failed`。

---

## 2. 架构设计原则

1. **优先使用 Pydantic AI 原生 `AbstractCapability` 错误 Hook**：
   - 直接实现 `AbstractCapability` 原生方法：`on_tool_execute_error`、`on_model_request_error`、`on_run_error`。
   - 利用 `CombinedCapability` 的逆序错误派发机制（`for capability in reversed(self._dispatch)`），将 `RunErrorReportingHook.hook_priority` 设为 `HookPriority.OBSERVABILITY (30)`，确保其在业务/持久化 Hook 之后观察到最终异常并记录结构化 Logfire 日志，然后原样 `raise` 交由运行时边界收敛状态。
2. **禁止掩盖错误的兜底文案（Fail-Closed & Error Transparency）**：
   - 正常运行时产生的所有 `Exception` 均必须在 `scheduler._run_worker` / `router.handle_user_message` 的 `except Exception as exc:` 分支中被捕获并持久化真实错误详情（`format_exception_detail(exc)`）。
   - 删除 `lock_queue.py` 中遗留的 `format_worker_error`，全局统一使用 `agent.hooks.format_exception_detail`。
3. **`run.failed` 单次发射不变量（Single-Emission Guarantee）**：
   - `SuperAgentRouter.handle_user_message` 在 `self._agent.run` 抛错并发射 `run.failed` 后，通过 Python 原生 `exc.add_note("_run_failed_emitted")` 标记异常并重抛；
   - `routes_chat.py` 的 `_bg_run()` 捕获 `Exception` 时检查 `"_run_failed_emitted" in getattr(exc, "__notes__", ())`：若已由 Router 发射，则仅静默消费避免 `asyncio.Task` 未检索异常警告，不再重复打日志或重复发射第二次 `run.failed`；若异常发生在进入 `self._agent.run` 之前（如前置语义路由或加载会话失败），则由 `_bg_run()` 兜底记录并发射 `run.failed`。

---

## 3. 核心组件规范

### 3.1 `format_exception_detail(exc: BaseException) -> str`（`src/agent/hooks/error_hook.py`）
- 对单个异常：若 `str(exc).strip()` 非空，格式化为 `"{TypeName}: {detail}"`；若为空（如 `ConnectError('')`），格式化为 `"{TypeName}"`。
- 若 `exc.__cause__` 或 `exc.__context__` 存在且格式化结果与主异常不同，追加 `" (cause: {CauseSummary})"`，保留底层传输/系统错误根因。

### 3.2 `RunErrorReportingHook`（`src/agent/hooks/error_hook.py`）
- 继承 `AbstractCapability[object]`，`hook_priority = int(HookPriority.OBSERVABILITY)`（`30`）。
- 会话 ID 动态解析：优先使用构造参数 `conversation_id`；若为空（如 `SuperAgentRouter` 单例 Agent），自动从 `ctx.deps.main_conversation_id` 提取当前主会话 ID。
- 实现三个原生 Hook：
  - `on_tool_execute_error`: 记录 `agent_name`, `conversation_id`, `task_id`, `role_name`, `run_id`, `tool_name`, `tool_call_id`, `error_type`, `error` 并重抛 `error`。
  - `on_model_request_error`: 记录 `agent_name`, `conversation_id`, `task_id`, `role_name`, `run_id`, `model_name`, `error_type`, `error` 并重抛 `error`。
  - `on_run_error`: 过滤 `(asyncio.CancelledError, RunCancelled)` 后记录 `agent_name`, `conversation_id`, `task_id`, `role_name`, `run_id`, `error_type`, `error` 并重抛 `error`。

### 3.3 四大运行边界收敛

| 边界 | 位置 | 捕获策略 | 状态与事件收敛 |
| --- | --- | --- | --- |
| **Role Worker** | `src/agent/core/scheduler.py` (`_run_worker`) | `except (asyncio.CancelledError, RunCancelled)` 与 `except Exception as exc:` | `result_summary = format_exception_detail(exc)` 更新工单为 `failed`；调用 `emit_worker_outcome(..., outcome="failed")` 同时发射 `run.failed` + `ticket.status_changed`；向主会话投递含真实错误摘要的 `pending_event`；正常收敛不向外抛出至 `asyncio.Task`。 |
| **Router 主对话** | `src/agent/core/router.py` (`handle_user_message` / `_run_proactive`) | `except RunCancelled` 与 `except Exception as exc:` | 发射 `run.failed(error_code=type(exc).__name__, error_message=format_exception_detail(exc))`，标记 `exc._run_failed_emitted = True` 后重抛给调用方。 |
| **HTTP 后台任务** | `src/agent/server/routes_chat.py` (`_bg_run`) | `except Exception as exc:` | 若 `exc._run_failed_emitted` 为真则直接收敛；否则记录 Logfire 错误并发射 `run.failed`。 |
| **HTTP 同步路由** | `src/agent/server/app.py` (`_handle_internal_error`) | Starlette `Exception` 全局处理器 | 记录 `Unhandled HTTP server exception` 并返回 `500 Internal Server Error` 标准 `ErrorEnvelope(code="internal_error", message=format_exception_detail(exc))`。 |
