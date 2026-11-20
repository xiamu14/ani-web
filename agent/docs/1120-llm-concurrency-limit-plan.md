# 并发任务下 LLM 请求限流、双池隔离与 429 协同退避实施计划（Plan）

- **基线 Commit**：`19824fe`
- **对应设计规范**：[1120-llm-concurrency-limit-spec.md](./1120-llm-concurrency-limit-spec.md)
- **实施原则**：小步快走、强解耦、单文件 `< 500` 行、每一步均可独立测试验证。

---

## 一、 分步实施 TODO 清单（Step 10.1 – Step 10.4）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 10.1** | `config/settings.py`、`llm/rate_limiter.py` | **Layer 0 双池并发控制器与 429 协同冷却门**：扩展 `LlmSettings` 限流配置；实现 `RateLimitCooldownGate`（`Retry-After` 解析 + 指数退避 + 全局单调时钟协同暂停）、`CooldownRetryModel(WrapperModel)` 与 `LlmConcurrencyController`（基于 Pydantic AI `ConcurrencyLimitedModel` 实现 Router 快车道池 + Role 共享池、幂等防嵌套包装及 `peak_in_flight` 实时并发水位统计） | `tests/test_llm_concurrency_limit.py` (Part 1: 双池并发上限、幂等包装与 429 协同冷却单测) | `[x]` DONE |
| **Step 10.2** | `llm/jev_client.py`、`llm/__init__.py` | **Layer 1 `JevHttpClient` 信号量限流、连接池复用与 429/503 退避**：为 `JevHttpClient` 增加 `asyncio.Semaphore(max_concurrency)`、长连接池复用（`aclose()`）、`429/502/503` 自动读取 `Retry-After` 并联动 `RateLimitCooldownGate` 重试 | `tests/test_llm_concurrency_limit.py` (Part 2: Jev 并发扇出限流与 429 恢复单测) | `[x]` DONE |
| **Step 10.3** | `core/roles.py`、`core/scheduler.py`、`core/runtime.py` | **Layer 2 运行时全链路装配**：`open_runtime` 创建 `LlmConcurrencyController`（挂载到 `SuperAgentRuntime.llm_controller`）；前台 `SuperAgentRouter` 绑定 `wrap_router_model` 快车道；`WorkspaceLockManager` 与 `RoleSpec.build_agent` 将所有 Role Worker 模型统一纳入 `wrap_role_model` 共享池；退出时关闭 Jev HTTP 连接池 | `tests/test_llm_concurrency_limit.py` (Part 3: `open_runtime` 下 10+ 并发 Role 任务 + Router 快车道零饥饿集成测试) | `[x]` DONE |
| **Step 10.4** | 全量 `src/` 与 `tests/` | **全量回归与代码质量门禁**：验证所有源文件 `< 500` 行、`basedpyright src/ tests/test_llm_concurrency_limit.py` (`0 errors`)、`ruff check src/ tests/` (`All checks passed!`) 及全仓测试套件通过 | 全量 `unittest` 套件 | `[x]` DONE |

---

## 二、 各步可测试验收标准

1. **Step 10.1（双池并发控制与 429 协同冷却门验证）**：
   - **并发水位硬边界**：配置 `role_max_concurrency=3`，同时发起 12 个并发 Model 请求（每个请求模拟耗时 50ms），实测 `controller.role_stats.peak_in_flight` **严格等于 `3`**（绝不超过 3），且 12 个请求全部成功完成；
   - **前台 Router 快车道零饥饿**：当 10 个后台 Role 请求占满 `role_limiter(max_running=2)` 并排队时，前台 Router 通过 `router_limiter(max_running=2)` 发起请求，**无需等待后台队列排空即可立即并发执行并响应**；
   - **429 协同退避零惊群**：当第 1 个请求遇到上游 `ModelHTTPError(status_code=429)` 时，`RateLimitCooldownGate` 触发全局冷却，后续并发请求自动等待冷却结束后再发出，最终在重试预算内 100% 恢复成功；
   - **幂等防死锁**：对同一个 Model 连续调用两次 `wrap_role_model(wrap_role_model(m))` 不会嵌套两层 `ConcurrencyLimitedModel`，不会触发 Pydantic AI 的嵌套死锁 `UserError`。
2. **Step 10.2（`JevHttpClient` 并发限流与 429 重试验证）**：
   - 配置 `max_concurrency=2`，同时发起 8 个 `request_decisions` 请求，Mock Transport 实测同时在途 HTTP 请求峰值 **严格 $\le 2$**；
   - 当 Mock Transport 前 2 次返回 `HTTP 429`（带 `Retry-After: 0.05`）且第 3 次返回 `200 OK` 时，`JevHttpClient` 自动等待并重试成功。
3. **Step 10.3（Runtime 多 Role 并发限流端到端验证）**：
   - 在 `open_runtime` 中配置 `llm_max_concurrency=2`，同时派发 6 个 `researcher`（`shared` 锁，允许 6 个 Worker 协程同时运行），实测所有 Worker 共享同一个 `role-llm-pool`，任意时刻实际发往底层 Model 的并发请求峰值 **严格 $\le 2$**，且 6 个任务全部 `completed`。
