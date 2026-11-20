"""Step 9.1–9.3: Agent Loop 安全退出边界（Max Turn/Token Budget 软硬阈值、Doom-Loop 熔断、TaskOutcomeJudge、异常隔离）单测。"""

from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage

from agent.core import RoleRegistry, RoleSpec, open_runtime
from agent.core.budget import (
    READONLY_ROLE_BUDGET_POLICY,
    READWRITE_ROLE_BUDGET_POLICY,
    ROUTER_BUDGET_POLICY,
    LoopBudgetPolicy,
    LoopSafetyAbortError,
    default_role_budget_policy,
)
from agent.core.outcome_judge import (
    ToolCallRecord,
    ToolExecutionTrace,
    evaluate_task_outcome,
    is_command_output_failed,
)
from agent.hooks import LoopSafetyGuardHook, PermissionGuardHook, TaskSteerHook
from agent.memory import derive_project_key
from agent.permissions import PermissionReviewer


class TestStep91BudgetAndOutcomeJudge(unittest.TestCase):
    """Step 9.1: 纯预算策略模型与 TaskOutcomeJudge 5 类场景分类验证。"""

    def test_budget_policy_limits_and_soft_threshold_detection(self) -> None:
        policy = LoopBudgetPolicy(
            max_requests=10,
            max_tool_calls=20,
            max_total_tokens=100_000,
            max_output_tokens=20_000,
            soft_limit_ratio=0.80,
        )
        limits = policy.to_usage_limits()
        self.assertEqual(limits.request_limit, 10)
        self.assertEqual(limits.tool_calls_limit, 20)
        self.assertEqual(limits.total_tokens_limit, 100_000)
        self.assertEqual(limits.output_tokens_limit, 20_000)
        self.assertEqual(policy.soft_request_threshold, 8)
        self.assertEqual(policy.soft_tool_calls_threshold, 16)
        self.assertEqual(policy.soft_total_tokens_threshold, 80_000)

        # 未达 80%
        u_low = RunUsage(requests=5, tool_calls=6, input_tokens=30_000, output_tokens=5_000)
        self.assertFalse(policy.is_soft_limit_reached(u_low))
        # Turn 达 80%
        self.assertTrue(policy.is_soft_limit_reached(u_low, next_request_index=8))
        # Token 达 80%
        u_high_tok = RunUsage(requests=3, tool_calls=4, input_tokens=70_000, output_tokens=12_000)
        self.assertTrue(policy.is_soft_limit_reached(u_high_tok))

        # 默认角色策略映射
        self.assertEqual(default_role_budget_policy("researcher"), READONLY_ROLE_BUDGET_POLICY)
        self.assertEqual(default_role_budget_policy("coder"), READWRITE_ROLE_BUDGET_POLICY)
        self.assertEqual(ROUTER_BUDGET_POLICY.max_requests, 12)

    def test_outcome_judge_five_scenarios(self) -> None:
        # 1. 正常完成 -> succeeded=True, allow_verified_memory=True
        trace_ok = ToolExecutionTrace(
            records=[
                ToolCallRecord(tool_name="edit_file", canonical_args='{"path":"a.py"}', succeeded=True),
                ToolCallRecord(
                    tool_name="run_command",
                    canonical_args='{"cmd":"pytest"}',
                    succeeded=True,
                    is_command_tool=True,
                    command_failed=False,
                ),
            ]
        )
        v_ok = evaluate_task_outcome("已完成修复并通过测试。", trace=trace_ok, role_name="coder")
        self.assertTrue(v_ok.succeeded)
        self.assertEqual(v_ok.status, "completed")
        self.assertTrue(v_ok.allow_verified_memory)

        # 2. 显式失败/阻塞标签 -> succeeded=False, allow_verified_memory=False
        v_tag = evaluate_task_outcome(
            "尝试修改配置文件，但缺少必要密钥 [TASK_STATUS: BLOCKED]",
            trace=trace_ok,
            role_name="coder",
        )
        self.assertFalse(v_tag.succeeded)
        self.assertEqual(v_tag.status, "failed")
        self.assertEqual(v_tag.reason_code, "explicit_status_tag_failed")
        self.assertFalse(v_tag.allow_verified_memory)

        # 3. 权限拒绝后无成功工具调用 -> unresolved_permission_block
        trace_perm = ToolExecutionTrace(permission_denials=1)
        v_perm = evaluate_task_outcome("权限被拒绝，我已停止操作。", trace=trace_perm, role_name="coder")
        self.assertFalse(v_perm.succeeded)
        self.assertEqual(v_perm.reason_code, "unresolved_permission_block")
        self.assertFalse(v_perm.allow_verified_memory)

        # 4. 末端命令非零退出码且未修复 -> unresolved_command_failure
        self.assertTrue(is_command_output_failed("Command exited with code 1\nFAILED (errors=2)"))
        trace_cmd_fail = ToolExecutionTrace(
            records=[
                ToolCallRecord(
                    tool_name="run_command",
                    canonical_args='{"cmd":"pytest"}',
                    succeeded=True,
                    is_command_tool=True,
                    command_failed=True,
                )
            ]
        )
        v_cmd = evaluate_task_outcome(
            "执行测试返回 Command exited with code 1，任务执行失败。",
            trace=trace_cmd_fail,
            role_name="tester",
        )
        self.assertFalse(v_cmd.succeeded)
        self.assertEqual(v_cmd.reason_code, "unresolved_command_failure")
        self.assertFalse(v_cmd.allow_verified_memory)

        # 5. 软预算触发后未完成 & 口头失败
        trace_soft = ToolExecutionTrace(soft_limit_triggered=True)
        v_soft = evaluate_task_outcome("由于预算限制未能完成剩余模块的迁移。", trace=trace_soft)
        self.assertFalse(v_soft.succeeded)
        self.assertEqual(v_soft.reason_code, "soft_budget_incomplete")

        v_verbal = evaluate_task_outcome("抱歉，找不到目标代码，无法完成该任务。")
        self.assertFalse(v_verbal.succeeded)
        self.assertEqual(v_verbal.reason_code, "explicit_status_tag_failed")


class TestStep92LoopSafetyAndPermissionHooks(unittest.IsolatedAsyncioTestCase):
    """Step 9.2: LoopSafetyGuardHook (80% 软着陆、Doom-Loop 熔断)、PermissionGuardHook 二次拒权短路与 TaskSteerHook 限流。"""

    async def test_soft_budget_wrapup_warning_injected_at_80_percent(self) -> None:
        policy = LoopBudgetPolicy(max_requests=5, soft_limit_ratio=0.80)  # soft threshold = 4
        guard = LoopSafetyGuardHook(policy=policy)
        seen_wrapup_at_turn: int | None = None

        def model_fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal seen_wrapup_at_turn
            turn_no = sum(1 for m in messages if isinstance(m, ModelRequest))
            full_text = "\n".join(
                str(p.content)
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
                if isinstance(p, UserPromptPart)
            )
            if "<budget_wrapup_warning>" in full_text and seen_wrapup_at_turn is None:
                seen_wrapup_at_turn = turn_no
                return ModelResponse(
                    parts=[TextPart(content="Wrapped up gracefully [TASK_STATUS: COMPLETED]")]
                )
            return ModelResponse(
                parts=[ToolCallPart(tool_name="ping_tool", args={"step": turn_no})]
            )

        agent: Agent[object, str] = Agent(
            FunctionModel(model_fn),
            deps_type=object,
            capabilities=[guard],
        )

        @agent.tool_plain
        def ping_tool(step: int) -> str:
            return f"pong-{step}"

        res = await agent.run("Run until wrapup", deps=object(), usage_limits=policy.to_usage_limits())
        self.assertEqual(seen_wrapup_at_turn, 4)
        self.assertTrue(guard.trace.soft_limit_triggered)
        self.assertIn("[TASK_STATUS: COMPLETED]", res.output)

    async def test_doom_loop_aborts_on_3_consecutive_identical_calls_and_resets_on_edit(self) -> None:
        policy = LoopBudgetPolicy(max_identical_tool_streak=3)
        guard = LoopSafetyGuardHook(policy=policy)
        call_counter = 0

        def doom_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "nonexistent.py"})]
            )

        agent: Agent[object, str] = Agent(
            FunctionModel(doom_model),
            deps_type=object,
            capabilities=[guard],
        )

        @agent.tool_plain
        def read_file(path: str) -> str:
            nonlocal call_counter
            call_counter += 1
            return f"Error: {path} not found"

        with self.assertRaises(LoopSafetyAbortError) as ctx:
            _ = await agent.run("Read file repeatedly", deps=object())

        self.assertEqual(ctx.exception.kind, "doom_loop")
        self.assertEqual(ctx.exception.tool_name, "read_file")
        self.assertEqual(ctx.exception.streak, 3)
        # 第 3 次在 before_tool_execute 即被拦截，实际只执行了 2 次
        self.assertEqual(call_counter, 2)
        self.assertTrue(guard.trace.doom_loop_aborted)

    async def test_repeated_permission_denial_short_circuits_on_second_attempt(self) -> None:
        # 静态规则拒绝 rm -rf / (forbidden command)
        reviewer = PermissionReviewer()
        trace = ToolExecutionTrace()
        perm_hook = PermissionGuardHook(reviewer=reviewer, trace=trace)
        attempts = 0

        def stubborn_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal attempts
            attempts += 1
            return ModelResponse(
                parts=[ToolCallPart(tool_name="shell", args={"command": "rm -rf /"})]
            )

        agent: Agent[object, str] = Agent(
            FunctionModel(stubborn_model),
            deps_type=object,
            capabilities=[perm_hook],
            retries=3,
        )

        @agent.tool_plain
        def shell(command: str) -> str:
            return f"ran {command}"

        with self.assertRaises(LoopSafetyAbortError) as ctx:
            _ = await agent.run("Try forbidden shell", deps=object())

        self.assertEqual(ctx.exception.kind, "permission_denied")
        self.assertEqual(ctx.exception.tool_name, "shell")
        # 第 1 次抛 ModelRetry，第 2 次直接抛 LoopSafetyAbortError 短路，不会耗到 4 次（1 + 3 retries）
        self.assertEqual(attempts, 2)
        self.assertEqual(trace.permission_denials, 2)

    async def test_steer_hook_bounded_by_max_injections(self) -> None:
        q: deque[str] = deque([f"steer-{i}" for i in range(10)])
        steer_hook = TaskSteerHook(q, max_injections=3)

        def simple_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="done")])

        agent: Agent[object, str] = Agent(
            FunctionModel(simple_model),
            deps_type=object,
            capabilities=[steer_hook],
        )
        _ = await agent.run("hello", deps=object())
        self.assertEqual(steer_hook.injected_count, 3)
        self.assertEqual(len(q), 7)


class TestStep93SchedulerAndRouterSafeExitIntegration(unittest.IsolatedAsyncioTestCase):
    """Step 9.3: Scheduler 伪完成拦截（保护下游依赖 + 阻止污染 Verified Memory）、Doom-Loop 落盘恢复与 _emit 异常隔离。"""

    async def test_pseudo_completion_blocks_downstream_and_prevents_memory_poisoning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace"
            ws.mkdir()
            db_path = Path(tmp) / "safe_exit.db"
            downstream_executed = False

            # 上游 coder 模拟“伪完成”：没有抛 Python 异常，但输出声明因阻塞未完成 [TASK_STATUS: BLOCKED]
            def pseudo_done_coder(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                return ModelResponse(
                    parts=[
                        TextPart(
                            content="尝试更新配置，但缺少生产环境密钥，无法完成该任务 [TASK_STATUS: BLOCKED]"
                        )
                    ]
                )

            def downstream_tester(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal downstream_executed
                downstream_executed = True
                return ModelResponse(parts=[TextPart(content="Tests executed")])

            # doom_role 模拟陷入连续相同工具调用死循环
            def doom_researcher(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="list_files", args={"dir": "missing"})]
                )

            from pydantic_ai.toolsets import FunctionToolset

            doom_ts: FunctionToolset[object] = FunctionToolset()

            @doom_ts.tool_plain
            def list_files(dir: str) -> str:
                return f"no such dir: {dir}"

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="You are coder.",
                        model=FunctionModel(pseudo_done_coder),
                        default_lock_mode="exclusive",
                    ),
                    RoleSpec(
                        name="tester",
                        description="tester",
                        instructions="You are tester.",
                        model=FunctionModel(downstream_tester),
                        default_lock_mode="exclusive",
                    ),
                    RoleSpec(
                        name="researcher",
                        description="researcher",
                        instructions="You are researcher.",
                        model=FunctionModel(doom_researcher),
                        toolsets=(doom_ts,),
                        default_lock_mode="shared",
                        budget_policy=LoopBudgetPolicy(max_identical_tool_streak=3),
                    ),
                ]
            )

            async with open_runtime(
                db_path=db_path,
                step_db_path=Path(tmp) / "steps.db",
                role_registry=registry,
                global_mcp_path=Path(tmp) / "no_global_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                main_id = conv.conversation_id

                # 注入一个会抛异常的 event handler，验证 scheduler._emit 100% 异常隔离不崩
                async def broken_event_handler(_cid: str, _summary: str, _kind: str) -> None:
                    raise ModelAPIError(model_name="router", message="Simulated proactive API glitch")

                rt.scheduler.set_event_handler(broken_event_handler)

                # 1. 派发上游 @T1 (伪完成) 与依赖 @T1 的下游 @T2
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=main_id,
                    role_name="coder",
                    mandate="Update prod config",
                    work_path=str(ws),
                )
                r2 = await rt.scheduler.dispatch_task(
                    main_conversation_id=main_id,
                    role_name="tester",
                    mandate="Run prod tests after T1",
                    work_path=str(ws),
                    depends_on=(r1.task_id,),
                )
                # 2. 派发无依赖的 @T3 (测试 Doom-Loop 熔断退出 + 锁释放)
                r3 = await rt.scheduler.dispatch_task(
                    main_conversation_id=main_id,
                    role_name="researcher",
                    mandate="Scan missing dir",
                    work_path=str(ws),
                )

                await rt.scheduler.wait_all()

                t1_row = await rt.ticket_store.get_ticket(main_id, r1.task_id)
                t2_row = await rt.ticket_store.get_ticket(main_id, r2.task_id)
                t3_row = await rt.ticket_store.get_ticket(main_id, r3.task_id)
                assert t1_row is not None and t2_row is not None and t3_row is not None

                # 断言 A: 伪完成的 @T1 被 TaskOutcomeJudge 拦截标为 failed
                self.assertEqual(t1_row.status, "failed")
                # 断言 B: 依赖 @T1 的下游 @T2 保持 queued，绝不被错误启动
                self.assertEqual(t2_row.status, "queued")
                self.assertFalse(downstream_executed)
                # 断言 C: 伪完成绝不写入 Verified Observation 污染 Memory
                proj_key = derive_project_key(str(ws))
                obs_list = await rt.memory_store.list_recent_observations(project_key=proj_key)
                self.assertEqual(len(obs_list), 0)
                # 断言 D: 陷入死循环的 @T3 在 3 步被 LoopSafetyAbortError 熔断并落盘为 failed，且释放工作区锁
                self.assertEqual(t3_row.status, "failed")
                self.assertIn("Doom loop detected", t3_row.result_summary or "")


if __name__ == "__main__":
    _ = unittest.main()
