# 权限控制与 Agent Loop 内 Tools 任务队列实施计划（Plan）

本文依据 [权限控制设计规范](./1118-permission-control.spec.md) 与 [现有工具清单](./1118-tools-table.md)，将实现拆分为 5 个“小步快走、单步可独立测试”的增量步骤，并严格遵守单文件 `< 500` 行及强解耦边界约束。

---

## 一、 分步实施清单（TODO 状态）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 5.1** | `llm/`、`permissions/` | 抽离公共 `JevDecisionClient`（保持 `semantic_router` 兼容）；实现 `permissions/rules.py`（单一 Mode、仅 `FORBIDDEN` 与 `REVIEW` 两类纯静态规则及 `evaluate_tool_rule`） | `tests/test_permission_rules.py` | `[x]` 已完成 |
| **Step 5.2** | `permissions/` | 实现 `permissions/reviewer.py`：`UserApprovalPort` 批量确认端口与 `PermissionReviewer`（`FORBIDDEN` 直拒，`REVIEW` 结合 `user_intent` 调 `Jev` 三态定级，微批合并确认 + `run_id` 同类临时授权） | `tests/test_permission_reviewer.py` | `[x]` 已完成 |
| **Step 5.3** | `hooks/` | 实现 `hooks/priority.py`（`HookPriority`: `PERMISSION=0 < POLICY=10 < TOOL_QUEUE=20 < OBSERVABILITY=30 < PERSISTENCE=90` 与稳定排序 `sort_capabilities`）及 `hooks/permission_hook.py`（`PermissionGuardHook.before_tool_execute`） | `tests/test_permission_hook.py` | `[x]` 已完成 |
| **Step 5.4** | `tools/` | 实现 `tools/tool_queue.py`：资源键解析 `resolve_tool_resource`（`path:` 含 `paths_overlap` / `ws:` / `cmd:` / `conv:` / `ticket:` / `none`）与 `ToolExecutionQueueHook.wrap_tool_execute` 读写锁排队 | `tests/test_tool_queue_hook.py` | `[x]` 已完成 |
| **Step 5.5** | `core/` | 在 `RoleSpec.build_agent`、`SuperAgentRouter` 与 `open_runtime` 通过 `sort_capabilities` 装配 Hook，注入 `Jev` 与可选 `UserApprovalPort` | 真实 LLM E2E（权限拦截 + 多工具队列 + Logfire Span） | `[x]` 已完成 |

---

## 二、 各步验收标准

* **Step 5.1（公共 Jev 客户端与静态 Rules）**：
  * `JevDecisionClient` 从 `context/semantic_router.py` 抽离至独立公共模块 `src/agent/llm/jev_client.py` 并通过 `semantic_router.py` 重导出保持兼容；
  * `permissions/rules.py` 零 IO / 零 Runtime 依赖，`evaluate_tool_rule` 精确覆盖 `FORBIDDEN`（高危命令、越界写删、读取凭证/私钥）、`REVIEW`（工作区外只读、核心配置修改、包安装/网络命令、DB 写等）与默认 `PASS`。
* **Step 5.2（Jev 审核器与用户确认端口）**：
  * 仅对 `REVIEW` 命中调用 `Jev`（`ALLOW` / `CONFIRM_USER` / `DENY`），`FORBIDDEN` 不触发模型调用直接拒绝；`CONFIRM_USER` 异步阻塞等待 `UserApprovalPort`。
* **Step 5.3（Hook 优先级与拦截）**：
  * `PermissionGuardHook` 优先级 `0` 在 `before_tool_execute` 永远最先运行，拒绝时抛出 `ModelRetry` 短路后续 Hook 与工具。
* **Step 5.4（Agent Loop 内 Tools 队列）**：
  * 同轮并发 `ToolCallPart` 按工具清单表执行 `shared` 并发读、`exclusive` 串行写与工作区 `shell` 独占，且权限拒绝时不占锁。
* **Step 5.5（运行时装配与 E2E）**：
  * 全链路接入 `open_runtime` 与 `RoleSpec` / `SuperAgentRouter`，并通过真实 LLM E2E 与 Logfire 验证。
