"""Step 9.4: Agent Loop Safe Exit 全链路 E2E 测试套件（12 个独立端到端场景，覆盖 open_runtime + Router + Scheduler + Memory）。

运行方式：
    .venv/bin/python -m unittest tests/e2e_loop_safe_exit.py -v
    或
    .venv/bin/python -m tests.e2e_loop_safe_exit
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import override

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from agent.core import (
    LoopBudgetPolicy,
    RoleRegistry,
    RoleSpec,
    open_runtime,
)
from agent.memory import derive_project_key
from agent.permissions import (
    ApprovalDecision,
    ApprovalItem,
    PermissionReviewer,
    UserApprovalPort,
)


def _build_workspace_toolset(ws_root: Path) -> FunctionToolset[object]:
    ts: FunctionToolset[object] = FunctionToolset()

    @ts.tool_plain
    def read_file(path: str) -> str:
        target = ws_root / path
        if not target.is_file():
            return f"Error: {path} does not exist"
        return target.read_text(encoding="utf-8")

    @ts.tool_plain
    def edit_file(path: str, replacement: str) -> str:
        target = ws_root / path
        _ = target.write_text(replacement, encoding="utf-8")
        return f"Updated {path}"

    @ts.tool_plain
    def run_command(command: str) -> str:
        if "fail" in command:
            return "Command exited with code 1\nFAILED (errors=1)"
        return f"Command exited with code 0: {command}"

    @ts.tool_plain
    def shell(command: str) -> str:
        return f"Executed: {command}"

    return ts


class _AlwaysDenyApprovalPort(UserApprovalPort):
    def __init__(self) -> None:
        self.approval_requests: int = 0

    @override
    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision:
        _ = (items, user_intent)
        self.approval_requests += 1
        return ApprovalDecision(approved=False, reason="Denied by security policy in E2E test")


class TestE2ELoopSafeExitSuite(unittest.IsolatedAsyncioTestCase):
    """12 个端到端（E2E）Safe Exit 场景，通过完整 open_runtime 检验所有 Agent Loop 退出边界。"""

    async def test_e2e_01_max_turn_soft_wrapup_completes_gracefully(self) -> None:
        """Case 01: Max Turn 80% 软着陆预警触发 -> Role 在撞上硬上限前优雅收尾并标为 completed。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            _ = (ws / "app.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
            seen_wrapup = False

            def model_fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal seen_wrapup
                turn = sum(1 for m in messages if isinstance(m, ModelRequest))
                prompts = "\n".join(
                    str(p.content)
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, UserPromptPart)
                )
                if "<budget_wrapup_warning>" in prompts:
                    seen_wrapup = True
                    return ModelResponse(
                        parts=[TextPart(content="Finished audit before turn cap [TASK_STATUS: COMPLETED]")]
                    )
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="read_file", args={"path": f"step_{turn}.py"})]
                )

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="researcher",
                        instructions="Audit files",
                        model=FunctionModel(model_fn),
                        toolsets=(_build_workspace_toolset(ws),),
                        default_lock_mode="shared",
                        budget_policy=LoopBudgetPolicy(max_requests=5, soft_limit_ratio=0.80),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                receipt = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="researcher",
                    mandate="Audit all files",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t = await rt.ticket_store.get_ticket(conv.conversation_id, receipt.task_id)
                assert t is not None
                self.assertTrue(seen_wrapup)
                self.assertEqual(t.status, "completed")
                self.assertIn("[TASK_STATUS: COMPLETED]", t.result_summary or "")

    async def test_e2e_02_max_turn_hard_cutoff_marks_failed_and_releases_lock(self) -> None:
        """Case 02: 模型无视 80% 软着陆继续调工具 -> 触发 UsageLimitExceeded 硬上限标为 failed 并释放工作区锁。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()

            def runaway_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                turn = sum(1 for m in messages if isinstance(m, ModelRequest))
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="read_file", args={"path": f"file_{turn}.py"})]
                )

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="Runaway coder",
                        model=FunctionModel(runaway_model),
                        toolsets=(_build_workspace_toolset(ws),),
                        default_lock_mode="exclusive",
                        budget_policy=LoopBudgetPolicy(max_requests=3, soft_limit_ratio=0.80),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                receipt = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Infinite turns",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t = await rt.ticket_store.get_ticket(conv.conversation_id, receipt.task_id)
                assert t is not None
                self.assertEqual(t.status, "failed")
                self.assertIn("UsageLimitExceeded", t.result_summary or "")
                locks = await rt.scheduler.inspect_workspace_locks(str(ws))
                self.assertEqual(locks, [])

    async def test_e2e_03_token_budget_hard_limit_cutoff(self) -> None:
        """Case 03: 单任务累计 Token 超过 max_total_tokens -> 触发 UsageLimitExceeded 硬截断并安全标为 failed。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            # 写入一个大文本文件使得单轮读取后下一轮请求前/后超过 50 tokens 硬预算
            _ = (ws / "huge.txt").write_text("token_word " * 500, encoding="utf-8")

            def token_heavy_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                turn = sum(1 for m in messages if isinstance(m, ModelRequest))
                if turn == 1:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="read_file", args={"path": "huge.txt"})]
                    )
                return ModelResponse(parts=[TextPart(content="Should not reach if token limit works")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="researcher",
                        instructions="Read huge file",
                        model=FunctionModel(token_heavy_model),
                        toolsets=(_build_workspace_toolset(ws),),
                        default_lock_mode="shared",
                        budget_policy=LoopBudgetPolicy(
                            max_requests=10,
                            max_total_tokens=1,  # 1 token limit guarantees immediate cutoff after 1st response
                        ),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                receipt = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="researcher",
                    mandate="Read huge file",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t = await rt.ticket_store.get_ticket(conv.conversation_id, receipt.task_id)
                assert t is not None
                self.assertEqual(t.status, "failed")
                self.assertIn("UsageLimitExceeded", t.result_summary or "")

    async def test_e2e_04_doom_loop_aborts_on_identical_readonly_calls(self) -> None:
        """Case 04: 连续 3 次相同参数调用只读工具 -> LoopSafetyGuardHook 立即抛出 LoopSafetyAbortError(doom_loop)。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            exec_count = 0

            ts: FunctionToolset[object] = FunctionToolset()

            @ts.tool_plain
            def read_file(path: str) -> str:
                nonlocal exec_count
                exec_count += 1
                return f"Missing {path}"

            def doom_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="read_file", args={"path": "ghost.py"})]
                )

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="researcher",
                        instructions="Find ghost.py",
                        model=FunctionModel(doom_model),
                        toolsets=(ts,),
                        default_lock_mode="shared",
                        budget_policy=LoopBudgetPolicy(max_identical_tool_streak=3),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                receipt = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="researcher",
                    mandate="Read ghost.py",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t = await rt.ticket_store.get_ticket(conv.conversation_id, receipt.task_id)
                assert t is not None
                self.assertEqual(t.status, "failed")
                self.assertEqual(exec_count, 2)
                self.assertIn("Doom loop detected", t.result_summary or "")

    async def test_e2e_05_read_edit_read_cycle_resets_doom_streak_and_succeeds(self) -> None:
        """Case 05: read_file -> edit_file -> read_file -> edit_file -> read_file 正常迭代不被误判为 Doom Loop。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            target = ws / "counter.py"
            _ = target.write_text("COUNT = 1\n", encoding="utf-8")

            def iterative_coder(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                returns = [
                    p
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, ToolReturnPart)
                ]
                step = len(returns)
                if step in (0, 2, 4):
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="read_file", args={"path": "counter.py"})]
                    )
                if step in (1, 3):
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="edit_file",
                                args={"path": "counter.py", "replacement": f"COUNT = {step + 1}\n"},
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="Updated counter to 4 [TASK_STATUS: COMPLETED]")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="Update counter iteratively",
                        model=FunctionModel(iterative_coder),
                        toolsets=(_build_workspace_toolset(ws),),
                        default_lock_mode="exclusive",
                        budget_policy=LoopBudgetPolicy(max_identical_tool_streak=3),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                receipt = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Increment counter",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t = await rt.ticket_store.get_ticket(conv.conversation_id, receipt.task_id)
                assert t is not None
                self.assertEqual(t.status, "completed")
                self.assertEqual(target.read_text(encoding="utf-8"), "COUNT = 4\n")

    async def test_e2e_06_pseudo_completion_explicit_tag_blocks_downstream_and_memory(self) -> None:
        """Case 06: 显式状态标签 [TASK_STATUS: FAILED] 拦截伪完成 -> 冻结下游依赖且不写入 Verified Memory。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            _ = (ws / "service.py").write_text("def run(): pass\n", encoding="utf-8")
            downstream_ran = False

            def failing_coder(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                has_ret = any(
                    isinstance(p, ToolReturnPart)
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                )
                if not has_ret:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="read_file", args={"path": "service.py"})]
                    )
                return ModelResponse(
                    parts=[
                        TextPart(
                            content="Read service.py but upstream schema is missing [TASK_STATUS: FAILED]"
                        )
                    ]
                )

            def downstream_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal downstream_ran
                downstream_ran = True
                return ModelResponse(parts=[TextPart(content="Downstream done")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="coder",
                        model=FunctionModel(failing_coder),
                        toolsets=(_build_workspace_toolset(ws),),
                    ),
                    RoleSpec(
                        name="tester",
                        description="tester",
                        instructions="tester",
                        model=FunctionModel(downstream_model),
                    ),
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Modify service.py",
                    work_path=str(ws),
                )
                r2 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="tester",
                    mandate="Test service.py",
                    work_path=str(ws),
                    depends_on=(r1.task_id,),
                )
                await rt.scheduler.wait_all()
                t1 = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                t2 = await rt.ticket_store.get_ticket(conv.conversation_id, r2.task_id)
                assert t1 is not None and t2 is not None
                self.assertEqual(t1.status, "failed")
                self.assertEqual(t2.status, "queued")
                self.assertFalse(downstream_ran)
                obs = await rt.memory_store.list_recent_observations(
                    project_key=derive_project_key(ws)
                )
                self.assertEqual(len(obs), 0)

    async def test_e2e_07_unresolved_command_failure_marks_tester_failed(self) -> None:
        """Case 07: tester 角色最后一次 run_command 返回非零退出码且未修复即输出总结 -> 自动判定为 failed。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()

            def tester_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                has_ret = any(
                    isinstance(p, ToolReturnPart)
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                )
                if not has_ret:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="run_command", args={"command": "pytest --fail"})]
                    )
                return ModelResponse(
                    parts=[TextPart(content="Tests finished with Command exited with code 1.")]
                )

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="tester",
                        description="tester",
                        instructions="run tests",
                        model=FunctionModel(tester_model),
                        toolsets=(_build_workspace_toolset(ws),),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="tester",
                    mandate="Run unit tests",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t1 = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                assert t1 is not None
                self.assertEqual(t1.status, "failed")

    async def test_e2e_08_unresolved_permission_denial_with_zero_success_tools_marks_failed(self) -> None:
        """Case 08: 工具调用被权限拦截且后续无任何成功工具调用即退出 -> TaskOutcomeJudge 判定为 failed。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()

            def polite_blocked_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                turn = sum(1 for m in messages if isinstance(m, ModelRequest))
                if turn == 1:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="shell", args={"command": "rm -rf /"})]
                    )
                return ModelResponse(
                    parts=[TextPart(content="由于权限被拒无法继续执行清理操作。")]
                )

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="coder",
                        model=FunctionModel(polite_blocked_model),
                        toolsets=(_build_workspace_toolset(ws),),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                permission_reviewer=PermissionReviewer(),
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Clean system",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t1 = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                assert t1 is not None
                self.assertEqual(t1.status, "failed")

    async def test_e2e_09_repeated_permission_denial_short_circuits_without_reprompting_user(self) -> None:
        """Case 09: 需用户审批的危险命令被拒绝后，模型第 2 次重复调用同一命令 -> 0 重复弹窗直接抛 LoopSafetyAbortError。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            deny_port = _AlwaysDenyApprovalPort()
            reviewer = PermissionReviewer(approval_port=deny_port)

            def stubborn_coder(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="shell", args={"command": "pip install some-pkg"})]
                )

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="coder",
                        model=FunctionModel(stubborn_coder),
                        toolsets=(_build_workspace_toolset(ws),),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                permission_reviewer=reviewer,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Force push",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t1 = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                assert t1 is not None
                self.assertEqual(t1.status, "failed")
                self.assertIn("Permission repeatedly denied", t1.result_summary or "")
                # 用户审批端口仅在第 1 次被调用，第 2 次被 PermissionGuardHook 直接短路！
                self.assertEqual(deny_port.approval_requests, 1)

    async def test_e2e_10_wall_clock_timeout_interrupts_hung_worker_and_frees_workspace_lock(self) -> None:
        """Case 10: Role Worker 超过 wall_clock_timeout_sec -> 自动触发 TimeoutError 标为 failed，并释放锁让后续排队任务运行。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            second_task_ran = False

            ts: FunctionToolset[object] = FunctionToolset()

            @ts.tool_plain
            async def slow_op() -> str:
                await asyncio.sleep(2.0)
                return "done"

            def hung_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                return ModelResponse(parts=[ToolCallPart(tool_name="slow_op", args={})])

            def fast_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal second_task_ran
                second_task_ran = True
                return ModelResponse(parts=[TextPart(content="Fast task completed [TASK_STATUS: COMPLETED]")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="slow coder",
                        instructions="slow",
                        model=FunctionModel(hung_model),
                        toolsets=(ts,),
                        default_lock_mode="exclusive",
                        budget_policy=LoopBudgetPolicy(wall_clock_timeout_sec=0.15),
                    ),
                    RoleSpec(
                        name="reviewer",
                        description="fast reviewer",
                        instructions="fast",
                        model=FunctionModel(fast_model),
                        default_lock_mode="shared",
                    ),
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Slow task",
                    work_path=str(ws),
                )
                r2 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="reviewer",
                    mandate="Fast task after lock release",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t1 = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                t2 = await rt.ticket_store.get_ticket(conv.conversation_id, r2.task_id)
                assert t1 is not None and t2 is not None
                self.assertEqual(t1.status, "failed")
                self.assertEqual(t2.status, "completed")
                self.assertTrue(second_task_ran)

    async def test_e2e_11_revise_after_pseudo_completion_recovers_and_unblocks_downstream(self) -> None:
        """Case 11: 上游 @T1 伪完成进入 failed 冻结下游 @T2 -> 用户执行 steer_or_revise 修正 @T1 成功后，@T2 自动解锁并完成。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            coder_runs = 0
            downstream_ran = False

            def recoverable_coder(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal coder_runs
                coder_runs += 1
                if coder_runs == 1:
                    return ModelResponse(
                        parts=[TextPart(content="First attempt blocked [TASK_STATUS: BLOCKED]")]
                    )
                return ModelResponse(
                    parts=[TextPart(content="Second attempt succeeded after revise [TASK_STATUS: COMPLETED]")]
                )

            def downstream_tester(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal downstream_ran
                downstream_ran = True
                return ModelResponse(parts=[TextPart(content="Downstream tests passed [TASK_STATUS: COMPLETED]")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="coder",
                        model=FunctionModel(recoverable_coder),
                    ),
                    RoleSpec(
                        name="tester",
                        description="tester",
                        instructions="tester",
                        model=FunctionModel(downstream_tester),
                    ),
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Initial mandate",
                    work_path=str(ws),
                )
                r2 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="tester",
                    mandate="Run tests after coder",
                    work_path=str(ws),
                    depends_on=(r1.task_id,),
                )
                await rt.scheduler.wait_all()

                # 第一轮后：@T1 failed, @T2 仍为 queued
                t1_before = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                t2_before = await rt.ticket_store.get_ticket(conv.conversation_id, r2.task_id)
                assert t1_before is not None and t2_before is not None
                self.assertEqual(t1_before.status, "failed")
                self.assertEqual(t2_before.status, "queued")
                self.assertFalse(downstream_ran)

                # 对 @T1 执行 steer_or_revise 续跑
                _ = await rt.scheduler.steer_or_revise(
                    main_conversation_id=conv.conversation_id,
                    task_id=r1.task_id,
                    instruction="Provided missing config, please complete now",
                )
                await rt.scheduler.wait_all()

                t1_after = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                t2_after = await rt.ticket_store.get_ticket(conv.conversation_id, r2.task_id)
                assert t1_after is not None and t2_after is not None
                self.assertEqual(t1_after.status, "completed")
                self.assertEqual(t2_after.status, "completed")
                self.assertTrue(downstream_ran)

    async def test_e2e_12_router_proactive_report_api_error_graceful_fallback(self) -> None:
        """Case 12: 后台任务完成触发 Router 主动播报时遭遇 ModelAPIError -> 优雅降级输出原始通知且不阻塞后续任务。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            proactive_replies: list[str] = []

            def simple_coder(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                return ModelResponse(parts=[TextPart(content="Coder finished job [TASK_STATUS: COMPLETED]")])

            def flaky_router_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                raise ModelAPIError(model_name="router-model", message="503 Service Unavailable")

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="coder",
                        model=FunctionModel(simple_coder),
                    )
                ]
            )
            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                # 将 Router 底层 model 替换为会抛 ModelAPIError 的 FunctionModel 并绑定主动播报回调
                from agent.core import SuperAgentRouter

                async def capture_proactive(_cid: str, text: str) -> None:
                    proactive_replies.append(text)

                flaky_router = SuperAgentRouter(
                    model=FunctionModel(flaky_router_model),
                    context_tree=rt.context_tree,
                    conversation_store=rt.store,
                    task_dispatcher=rt.scheduler,
                    on_proactive_reply=capture_proactive,
                )
                async def forward_event(cid: str, summary: str, kind: str) -> str | None:
                    return await flaky_router.on_ticket_event(cid, summary, kind=kind)

                rt.scheduler.set_event_handler(forward_event)

                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)
                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Run job 1",
                    work_path=str(ws),
                )
                r2 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate="Run job 2",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()

                t1 = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                t2 = await rt.ticket_store.get_ticket(conv.conversation_id, r2.task_id)
                assert t1 is not None and t2 is not None
                self.assertEqual(t1.status, "completed")
                self.assertEqual(t2.status, "completed")
                self.assertEqual(len(proactive_replies), 2)
                self.assertIn("[后台任务通知]", proactive_replies[0])
                self.assertIn("Coder finished job", proactive_replies[0])


if __name__ == "__main__":
    _ = unittest.main()
