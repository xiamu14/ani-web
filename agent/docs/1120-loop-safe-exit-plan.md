# Agent Loop 安全退出边界（Safe Exit）与任务成功判定实施计划（Plan）

- **基线 Commit**：`76996c2`
- **对应设计规范**：[1120-loop-safe-exit-spec.md](./1120-loop-safe-exit-spec.md)
- **实施原则**：小步快走、强解耦、单文件 `< 500` 行、每一步可独立运行单测 + `basedpyright` + `ruff` 验证。

---

## 一、 分步实施 TODO 清单（Step 9.1 – Step 9.4）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 9.1** | `core/budget.py`、`core/outcome_judge.py` | **Layer 0 纯预算策略与任务完成判定器（零 IO）**：定义 `LoopBudgetPolicy`（含 `to_usage_limits()` 与软阈值计算）、`LoopSafetyAbortError`、`ToolExecutionTrace`、`TaskOutcomeVerdict` 与 `evaluate_task_outcome()`（覆盖显式状态标签、未解权限拒绝、末端命令非零退出码、软预算未完成、口头失败识别） | `tests/test_loop_safe_exit.py` (Part 1: 纯判定器与预算模型单测) | `[x]` DONE |
| **Step 9.2** | `hooks/loop_safety_hook.py`、`hooks/permission_hook.py`、`hooks/steer_hook.py`、`hooks/priority.py` | **Layer 1 运行中安全守卫 Hooks**：实现 `LoopSafetyGuardHook`（80% Turn/Token/ToolCall 软着陆 `<budget_wrapup_warning>` 注入、连续 $K=3$ 次相同 `(tool, args)` Doom-Loop 熔断、工具成功/失败轨迹采集）；升级 `PermissionGuardHook`（同一被拒 `(tool, args)` 二次调用立即抛 `LoopSafetyAbortError` 短路）；升级 `TaskSteerHook`（`max_injections` 有界限流） | `tests/test_loop_safe_exit.py` (Part 2: Hook 级软着陆、Doom-Loop 与重复拒权短路单测) | `[x]` DONE |
| **Step 9.3** | `core/roles.py`、`core/scheduler.py`、`core/router.py`、`core/lock_queue.py` | **Layer 2 运行时装配、伪完成拦截与异常隔离**：`RoleSpec` 支持 `budget_policy`；`WorkspaceLockManager._run_worker` 绑定 `usage_limits` + `timeout` + `LoopSafetyGuardHook`，在 `agent.run()` 后通过 `evaluate_task_outcome()` 判定真伪完成（伪完成标为 `failed`、冻结下游依赖、阻止污染 Verified Memory），并对 `_emit` 与 `TimeoutError` 做异常兜底；修复 `LockQueueState` 使被依赖阻塞的排队任务不饿死后续无依赖就绪任务；`SuperAgentRouter` 绑定 `ROUTER_BUDGET_POLICY` 并为 `_run_proactive_report` 增加异常降级回退 | `tests/test_loop_safe_exit.py` (Part 3: Scheduler + Router 全链路 Safe Exit 集成单测) | `[x]` DONE |
| **Step 9.4** | 全量 `src/` 与 `tests/` | **全量回归与代码质量门禁**：运行全量单测、确认所有源文件 `< 500` 行、`basedpyright src/ tests/test_loop_safe_exit.py` (`0 errors`)、`ruff check src/ tests/` (`All checks passed!`) | 全量 `unittest` 套件 | `[x]` DONE |

---

## 二、 各步可测试验收标准

1. **Step 9.1（预算策略与 `TaskOutcomeJudge` 纯函数验证）**：
   - `LoopBudgetPolicy.to_usage_limits()` 精确映射 `request_limit`、`tool_calls_limit`、`total_tokens_limit`、`output_tokens_limit`；
   - `evaluate_task_outcome()` 对以下 5 类场景 100% 准确分类：
     1. 正常完成（工具成功 + 正常总结或 `[TASK_STATUS: COMPLETED]`）$\rightarrow$ `succeeded=True, allow_verified_memory=True`；
     2. 显式失败标签（`[TASK_STATUS: FAILED]` / `[TASK_STATUS: BLOCKED]` / `[TASK_STATUS: INCOMPLETE]`）$\rightarrow$ `succeeded=False, allow_verified_memory=False`；
     3. 权限拒绝后无任何成功工具调用即退出 $\rightarrow$ `succeeded=False, reason_code="unresolved_permission_block"`；
     4. 末端 `shell` / `run_command` 返回非零退出码（如 `Command exited with code 1`）且未修复即退出 $\rightarrow$ `succeeded=False, reason_code="unresolved_command_failure"`；
     5. 零工具调用且输出口头报错（如 `"抱歉，我无法完成该任务"`）$\rightarrow$ `succeeded=False`。
2. **Step 9.2（`LoopSafetyGuardHook` 与 `PermissionGuardHook` 验证）**：
   - **80% 软着陆预警**：当 `max_requests=5, soft_limit_ratio=0.8` 时，在第 4 轮 `before_model_request` 自动注入 `<budget_wrapup_warning>`，引导模型在第 4 轮直接总结输出而非撞上第 5 轮硬抛 `UsageLimitExceeded`；
   - **Doom-Loop 熔断**：当模型连续 3 次以完全相同的参数 `{"path": "missing.py"}` 调用 `read_file` 时，第 3 次在 `before_tool_execute` 直接抛出 `LoopSafetyAbortError(kind="doom_loop")`，耗时从 50 步收敛为 3 步；若中间穿插了 `edit_file`/`write_file`，计数自动清零不误杀；
   - **重复权限拒绝短路**：当模型对已被 `PermissionGuardHook` 拒绝的同一 `(tool_name, args)` 发起第 2 次调用时，不再调用 `reviewer.authorize()`，直接抛出 `LoopSafetyAbortError(kind="permission_denied")`。
3. **Step 9.3（Scheduler 与 Router 全链路 Safe Exit 集成验证）**：
   - **伪完成拦截与下游保护**：上游任务 `@T1` 未抛异常但输出 `"由于权限不足，无法完成修改 [TASK_STATUS: BLOCKED]"`，Scheduler 必须将 `@T1` 标记为 `status="failed"`，依赖 `@T1` 的下游任务 `@T2` 保持 `queued` 不被错误启动，且 Memory Store 中不产生任何 `type="change"` 的错误记忆；
   - **超时与 Doom-Loop 恢复**：Worker 触发 `TimeoutError` 或 `LoopSafetyAbortError` 时，Ticket 状态准确落盘为 `failed`（含清晰原因摘要），工作区锁与临时权限 100% 释放，不阻塞同目录其他无依赖排队任务；
   - **Router 主动播报异常隔离**：当 `_run_proactive_report` 遭遇模型异常时，Scheduler 的 `_release_and_schedule_next` 不受影响，后续排队任务正常出队执行。
