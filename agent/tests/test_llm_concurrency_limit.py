"""Step 10.1–10.3: LLM 并发请求限流、双池隔离（Router 快车道 + Role 共享池）、429 协同冷却与 Jev 限流单测。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.core import RoleRegistry, RoleSpec, open_runtime
from agent.llm import JevHttpClient, LlmConcurrencyController


class TestStep101DualPoolConcurrencyAndCooldown(unittest.IsolatedAsyncioTestCase):
    """Step 10.1: 双池并发上限、Router 快车道零饥饿、429 协同退避冷却与幂等包装防死锁测试。"""

    async def test_role_shared_pool_strictly_caps_in_flight_concurrency_and_idempotent_wrap(self) -> None:
        controller = LlmConcurrencyController(
            role_max_concurrency=3,
            router_max_concurrency=2,
            base_delay_sec=0.02,
        )

        async def slow_role_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            await asyncio.sleep(0.04)
            return ModelResponse(parts=[TextPart(content="role-ok")])

        raw_model = FunctionModel(slow_role_model)
        # 连续 wrap 两次验证幂等，不会触发 Pydantic AI 的 Nested ConcurrencyLimitedModel UserError
        wrapped_once = controller.wrap_role_model(raw_model)
        wrapped_twice = controller.wrap_role_model(wrapped_once)
        self.assertIs(wrapped_once, wrapped_twice)

        # 同时启动 12 个绑定该共享池的 Agent 实例并发请求
        agents = [Agent(controller.wrap_role_model(raw_model)) for _ in range(12)]
        results = await asyncio.gather(*(a.run(f"task-{i}") for i, a in enumerate(agents)))

        self.assertEqual(len(results), 12)
        self.assertTrue(all(r.output == "role-ok" for r in results))
        self.assertEqual(controller.role_stats.total_requests, 12)
        # 12 个并发任务下，实际峰值在途请求严格 <= 3（且达到 3）
        self.assertEqual(controller.role_stats.peak_in_flight, 3)
        self.assertEqual(controller.role_stats.current_in_flight, 0)

    async def test_router_fast_lane_never_starved_by_saturated_role_pool(self) -> None:
        controller = LlmConcurrencyController(
            role_max_concurrency=1,
            router_max_concurrency=2,
        )
        role_gate = asyncio.Event()
        role_started = asyncio.Event()

        async def blocking_role_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            role_started.set()
            _ = await role_gate.wait()
            return ModelResponse(parts=[TextPart(content="role-done")])

        async def fast_router_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="router-fast-reply")])

        role_agent = Agent(controller.wrap_role_model(FunctionModel(blocking_role_model)))
        router_agent = Agent(controller.wrap_router_model(FunctionModel(fast_router_model)))

        # 启动 4 个后台 Role 请求把 role_limiter(max_running=1) 彻底占满并排队
        bg_tasks = [asyncio.create_task(role_agent.run(f"bg-{i}")) for i in range(4)]
        _ = await role_started.wait()

        # 此时后台池已被完全阻塞，前台 Router 发起请求必须零等待立即返回
        router_res = await asyncio.wait_for(router_agent.run("user interactive query"), timeout=0.5)
        self.assertEqual(router_res.output, "router-fast-reply")
        self.assertEqual(controller.router_stats.total_requests, 1)

        # 放行后台任务
        role_gate.set()
        bg_results = await asyncio.gather(*bg_tasks)
        self.assertEqual(len(bg_results), 4)

    async def test_429_rate_limit_triggers_global_cooldown_gate_and_recovers(self) -> None:
        controller = LlmConcurrencyController(
            role_max_concurrency=3,
            rate_limit_max_retries=3,
            base_delay_sec=0.02,
        )
        call_count = 0

        async def flaky_429_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ModelHTTPError(
                    status_code=429,
                    model_name="upstream-llm",
                    body={"error": "Too Many Requests - concurrent limit exceeded"},
                )
            return ModelResponse(parts=[TextPart(content="recovered-after-429")])

        agent = Agent(controller.wrap_role_model(FunctionModel(flaky_429_model)))
        results = await asyncio.gather(agent.run("req-1"), agent.run("req-2"))

        self.assertEqual([r.output for r in results], ["recovered-after-429", "recovered-after-429"])
        self.assertEqual(controller.cooldown_gate.total_429_hits, 2)
        self.assertEqual(controller.role_stats.rate_limit_retries, 2)
        self.assertGreaterEqual(controller.cooldown_gate.total_cooldown_waits, 1)


class TestStep102JevHttpClientConcurrencyAndRetry(unittest.IsolatedAsyncioTestCase):
    """Step 10.2: JevHttpClient 信号量限流、Retry-After 429 退避与连接池关闭测试。"""

    async def test_jev_client_caps_concurrency_and_recovers_from_429(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            _ = request
            attempts += 1
            await asyncio.sleep(0.03)
            if attempts <= 2:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0.02"},
                    json={"error": "rate limited"},
                )
            return httpx.Response(
                200,
                json={"model": "typesafe/jev-1.14", "answers": {"allow": True}},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            jev = JevHttpClient(
                api_key="test-key",
                http_client=http_client,
                max_concurrency=2,
                max_retries=3,
                base_delay_sec=0.01,
            )
            res = await asyncio.gather(
                *(
                    jev.request_decisions(state={"i": i}, questions={"allow": "bool"})
                    for i in range(6)
                )
            )
            self.assertEqual(len(res), 6)
            # 6 个并发请求 + 2 次 429 重试 = 8 次底层请求，但峰值在途严格 == 2
            self.assertEqual(jev.stats.peak_in_flight, 2)
            self.assertEqual(jev.stats.rate_limit_retries, 2)
            self.assertEqual(jev.cooldown_gate.total_429_hits, 2)
            await jev.aclose()


class TestStep103RuntimeMultiRoleConcurrencyIntegration(unittest.IsolatedAsyncioTestCase):
    """Step 10.3: open_runtime 下 6 个并发 shared-lock Role 任务统一受 LlmConcurrencyController(max=2) 节流。"""

    async def test_open_runtime_throttles_6_concurrent_shared_role_workers_to_max_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()

            async def slow_researcher(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                await asyncio.sleep(0.05)
                return ModelResponse(parts=[TextPart(content="Research done [TASK_STATUS: COMPLETED]")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="shared researcher",
                        instructions="research",
                        model=FunctionModel(slow_researcher),
                        default_lock_mode="shared",
                    )
                ]
            )
            custom_ctrl = LlmConcurrencyController(
                role_max_concurrency=2,
                router_max_concurrency=2,
            )

            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
                llm_controller=custom_ctrl,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                # 同工作区连续派发 6 个 shared 只读任务：WorkspaceLockManager 允许 6 个 Worker 同时处于 running 状态
                receipts = [
                    await rt.scheduler.dispatch_task(
                        main_conversation_id=conv.conversation_id,
                        role_name="researcher",
                        mandate=f"Concurrent research #{i}",
                        work_path=str(ws),
                    )
                    for i in range(6)
                ]
                self.assertTrue(all(r.status == "running" for r in receipts))
                await rt.scheduler.wait_all()

                for r in receipts:
                    t = await rt.ticket_store.get_ticket(conv.conversation_id, r.task_id)
                    assert t is not None
                    self.assertEqual(t.status, "completed")

                # 6 个 Worker 虽然同时 running，但发往 LLM 的实际并发峰值严格受限于 role_max_concurrency=2
                self.assertEqual(rt.llm_controller.role_stats.total_requests, 6)
                self.assertEqual(rt.llm_controller.role_stats.peak_in_flight, 2)
                self.assertEqual(rt.llm_controller.role_stats.current_in_flight, 0)


if __name__ == "__main__":
    _ = unittest.main()
