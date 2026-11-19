"""Step 5.3 单元测试: HookPriority 稳定排序与 PermissionGuardHook 最高优先级短路拦截。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from typing_extensions import override

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.step_persistence import SqliteStepStore, StepPersistence

from agent.hooks import HookPriority, PermissionGuardHook, sort_capabilities
from agent.llm import JevHttpClient
from agent.permissions import ApprovalDecision, ApprovalItem, PermissionReviewer


class _PolicyRecorderHook(AbstractCapability[object]):
    """模拟低优先级 before_tool_execute Hook，用于验证被 PermissionGuardHook 短路。"""

    hook_priority: int = int(HookPriority.POLICY)

    def __init__(self, trace: list[str]) -> None:
        self._trace: list[str] = trace

    @override
    async def before_tool_execute(
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        _ = (ctx, tool_def)
        self._trace.append(f"policy:{call.tool_name}")
        return args


class _FakeApproval:
    def __init__(self, approved: bool) -> None:
        self._approved: bool = approved
        self.calls: int = 0

    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision:
        _ = (items, user_intent)
        self.calls += 1
        return ApprovalDecision(approved=self._approved, reason="拒绝" if not self._approved else "允许")


def _mock_jev(choice: str) -> JevHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json={"answers": {"verdict": {"choice": choice}}})

    return JevHttpClient(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TestPermissionHookAndPriority(unittest.IsolatedAsyncioTestCase):
    """验证 sort_capabilities 优先级顺序与 PermissionGuardHook 拦截短路行为。"""

    def test_sort_capabilities_orders_permission_first_and_persistence_last(self) -> None:
        reviewer = PermissionReviewer()
        perm = PermissionGuardHook(reviewer=reviewer)
        policy = _PolicyRecorderHook([])
        persistence = StepPersistence(store=SqliteStepStore(database=":memory:"), agent_name="coder")

        # 故意以倒序传入
        ordered = sort_capabilities([persistence, policy, perm])
        self.assertIs(ordered[0], perm)
        self.assertIs(ordered[1], policy)
        self.assertIs(ordered[2], persistence)

    async def test_permission_hook_short_circuits_lower_hooks_and_tools_on_deny(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()

            trace: list[str] = []
            executed_cmds: list[str] = []
            toolset: FunctionToolset[object] = FunctionToolset()

            @toolset.tool_plain
            async def shell(command: str) -> str:
                executed_cmds.append(command)
                trace.append(f"tool:{command}")
                return f"ran:{command}"

            _ = shell

            reviewer = PermissionReviewer(
                jev_client=_mock_jev("CONFIRM_USER"),
                approval_port=_FakeApproval(approved=False),
                batch_window_seconds=0.005,
            )
            perm = PermissionGuardHook(reviewer=reviewer, workspace_root=ws, user_intent="初始化")
            policy = _PolicyRecorderHook(trace)

            def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = info
                has_retry = any(
                    isinstance(m, ModelRequest)
                    and any(isinstance(p, ToolReturnPart) and p.outcome == "retried" for p in m.parts)
                    for m in messages
                )
                has_return = any(
                    isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts)
                    for m in messages
                )
                if has_retry:
                    return ModelResponse(parts=[TextPart(content="已被权限拦截，停止执行。")])
                if not has_return:
                    return ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"command": "uv add httpx"})])
                return ModelResponse(parts=[TextPart(content="done")])

            # 即使故意把 policy 排在前面，sort_capabilities 也必须把 perm 放最前
            agent = Agent(
                model=FunctionModel(model_fn),
                deps_type=object,
                toolsets=[toolset],
                capabilities=sort_capabilities([policy, perm]),
            )
            res = await agent.run("初始化")
            self.assertIn("权限拦截", res.output)
            # 权限拒绝时，低优先级 Hook 和真实工具均不得执行
            self.assertEqual(trace, [])
            self.assertEqual(executed_cmds, [])

    async def test_permission_hook_passes_to_lower_hooks_when_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()

            trace: list[str] = []
            toolset: FunctionToolset[object] = FunctionToolset()

            @toolset.tool_plain
            async def read_file(path: str) -> str:
                trace.append(f"tool:{path}")
                return f"content:{path}"

            _ = read_file

            reviewer = PermissionReviewer(jev_client=_mock_jev("ALLOW"))
            perm = PermissionGuardHook(reviewer=reviewer, workspace_root=ws, user_intent="读源码")
            policy = _PolicyRecorderHook(trace)

            def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = info
                has_return = any(
                    isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts)
                    for m in messages
                )
                if not has_return:
                    return ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "main.py"})])
                return ModelResponse(parts=[TextPart(content="ok")])

            agent = Agent(
                model=FunctionModel(model_fn),
                deps_type=object,
                toolsets=[toolset],
                capabilities=sort_capabilities([policy, perm]),
            )
            res = await agent.run("读源码")
            self.assertEqual(res.output, "ok")
            self.assertEqual(trace, ["policy:read_file", "tool:main.py"])


if __name__ == "__main__":
    _ = unittest.main()
