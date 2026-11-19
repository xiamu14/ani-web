"""Step 6.2 单元测试: Tools 渐进披露（ToolSearch）、声明式延迟标记与权限/队列联动。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, override

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    ToolSearch,
    WrapToolExecuteHandler,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    ToolSearchReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.step_persistence import SqliteStepStore, StepPersistence

from agent.core import RoleSpec
from agent.extensions import (
    DeferToolsCapability,
    ToolDisclosurePolicy,
    bilingual_tool_search_fn,
    build_skills_capability,
    build_tool_search_capabilities,
)
from agent.hooks import PermissionGuardHook, sort_capabilities
from agent.permissions import PermissionReviewer
from agent.tools import ToolExecutionQueueHook


class TestToolSearchDisclosure(unittest.IsolatedAsyncioTestCase):
    """验证 ToolSearch 延迟加载、双语检索、Hook 优先级与权限/队列强联动。"""

    async def test_deferred_tools_hidden_initially_and_activated_on_search(self) -> None:
        ts: FunctionToolset[object] = FunctionToolset()

        @ts.tool_plain
        def read_file(path: str) -> str:
            """Read a workspace file."""
            return f"read:{path}"

        @ts.tool_plain
        def mcp_ping() -> str:
            """Ping MCP health endpoint."""
            return "pong"

        @ts.tool_plain
        def mcp_pg_query(sql: str) -> str:
            """Execute read-only SQL query against analytics Postgres."""
            return f"rows:{sql}"

        @ts.tool_plain(defer_loading=True)
        def write_file(path: str, content: str) -> str:
            """Write content to a file."""
            return f"wrote:{path}:{len(content)}"

        _ = (read_file, mcp_ping, mcp_pg_query, write_file)

        policy = ToolDisclosurePolicy(
            defer_patterns=("mcp_*",),
            eager_names=frozenset({"mcp_ping"}),
        )
        self.assertFalse(policy.should_defer(ToolDefinition(name="mcp_ping", description="p")))
        self.assertTrue(policy.should_defer(ToolDefinition(name="mcp_pg_query", description="q")))
        self.assertFalse(
            policy.should_defer(
                ToolDefinition(name="search_tools", description="s", tool_kind="tool-search")
            )
        )
        self.assertFalse(
            policy.should_defer(
                ToolDefinition(name="load_capability", description="l", tool_kind="capability-load")
            )
        )

        visible_per_turn: list[list[str]] = []
        turn = 0

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            _ = messages
            turn += 1
            visible_per_turn.append([t.name for t in info.function_tools])
            if turn == 1:
                # 未检索直接调用被隐藏的延迟工具，框架应阻断并要求先 search_tools
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="mcp_pg_query", args={"sql": "SELECT 1"})]
                )
            if turn == 2:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="search_tools", args={"queries": ["postgres"]})]
                )
            if turn == 3:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="mcp_pg_query", args={"sql": "SELECT 42"})]
                )
            return ModelResponse(parts=[TextPart(content="query-ok")])

        agent = Agent(
            FunctionModel(model_fn),
            deps_type=object,
            toolsets=[ts],
            capabilities=sort_capabilities(build_tool_search_capabilities(policy)),
        )
        res = await agent.run("查询分析库")
        self.assertEqual(res.output, "query-ok")

        # Turn 0/1：只有常驻工具（read_file, mcp_ping），延迟工具 mcp_pg_query 与 write_file 均隐藏
        self.assertEqual(sorted(visible_per_turn[0]), ["mcp_ping", "read_file"])
        # Turn 2 检索 postgres 后，Turn 3 仅激活 mcp_pg_query，write_file 依然保持隐藏
        self.assertEqual(sorted(visible_per_turn[2]), ["mcp_pg_query", "mcp_ping", "read_file"])

        # 确认框架记录了 ToolSearchReturnPart 且包含 mcp_pg_query
        has_search_return = any(
            isinstance(part, ToolSearchReturnPart) and "mcp_pg_query" in str(part.content)
            for msg in res.all_messages()
            for part in msg.parts
        )
        self.assertTrue(has_search_return)

    async def test_bilingual_tool_search_strategy_and_prefix_stability(self) -> None:
        ts: FunctionToolset[object] = FunctionToolset()

        @ts.tool_plain(defer_loading=True)
        def db_order_query(order_id: str) -> str:
            """查询订单数据库并返回订单详情。"""
            return f"order:{order_id}"

        @ts.tool_plain(defer_loading=True)
        def deploy_release_check(service: str) -> str:
            """检查服务发布部署就绪度。"""
            return f"deploy:{service}"

        _ = (db_order_query, deploy_release_check)

        seen_turns: list[tuple[list[str], str | None]] = []

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            _ = messages
            seen_turns.append(([t.name for t in info.function_tools], info.instructions))
            if len(seen_turns) == 1:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="search_tools", args={"queries": ["查询订单数据库"]})]
                )
            if len(seen_turns) == 2:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="db_order_query", args={"order_id": "ORD-9"})]
                )
            return ModelResponse(parts=[TextPart(content="ORD-9-found")])

        policy = ToolDisclosurePolicy(strategy=bilingual_tool_search_fn)
        agent = Agent(
            FunctionModel(model_fn),
            deps_type=object,
            toolsets=[ts],
            capabilities=sort_capabilities(build_tool_search_capabilities(policy)),
        )
        res = await agent.run("查询订单 ORD-9")
        self.assertEqual(res.output, "ORD-9-found")
        self.assertEqual(seen_turns[0][0], ["search_tools"])
        self.assertEqual(sorted(seen_turns[1][0]), ["db_order_query", "search_tools"])
        self.assertNotIn("deploy_release_check", seen_turns[1][0])
        # 激活前后 instructions 前缀不变
        self.assertEqual(seen_turns[0][1], seen_turns[1][1])

    async def test_sort_capabilities_permission_guard_and_queue_lock_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()

            policy = ToolDisclosurePolicy(defer_names=frozenset({"write_file"}))
            perm = PermissionGuardHook(reviewer=PermissionReviewer(), workspace_root=ws)
            queue = ToolExecutionQueueHook(workspace_root=ws)
            defer_cap, search_cap = build_tool_search_capabilities(policy)
            assert isinstance(defer_cap, DeferToolsCapability)
            assert isinstance(search_cap, ToolSearch)
            persistence = StepPersistence(store=SqliteStepStore(database=":memory:"), agent_name="coder")

            ordered = sort_capabilities([persistence, search_cap, defer_cap, queue, perm])
            self.assertEqual(ordered, [perm, defer_cap, queue, search_cap, persistence])

            ts: FunctionToolset[object] = FunctionToolset()
            executed_writes: list[str] = []
            read_active = False
            search_saw_write_active = False
            write_active = False

            @ts.tool_plain
            async def read_file(path: str) -> str:
                """Read workspace file."""
                nonlocal read_active
                read_active = True
                await asyncio.sleep(0.01)
                read_active = False
                return f"read:{path}"

            @ts.tool_plain
            async def write_file(path: str, content: str) -> str:
                """Write workspace file."""
                nonlocal write_active
                _ = content
                write_active = True
                executed_writes.append(path)
                await asyncio.sleep(0.05)
                write_active = False
                return f"wrote:{path}"

            _ = (read_file, write_file)

            class _SearchSpy(AbstractCapability[object]):
                hook_priority: int = 30

                @override
                async def wrap_tool_execute(  # pyright: ignore[reportAny]
                    self,
                    ctx: RunContext[object],
                    *,
                    call: ToolCallPart,
                    tool_def: ToolDefinition,
                    args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
                    handler: WrapToolExecuteHandler,
                ) -> Any:  # pyright: ignore[reportExplicitAny]
                    nonlocal search_saw_write_active
                    _ = (ctx, tool_def)
                    if call.tool_name == "search_tools":
                        await asyncio.sleep(0.01)
                        search_saw_write_active = write_active or read_active
                    return await handler(args)  # pyright: ignore[reportAny]

            step = 0

            def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                nonlocal step
                _ = info
                step += 1
                if step == 1:
                    # 1) 激活 write_file
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="search_tools", args={"queries": ["write_file"]})]
                    )
                if step == 2:
                    # 2) 激活后尝试越界写 /etc/passwd -> 必须被 PermissionGuardHook 拦截
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="write_file",
                                args={"path": "/etc/passwd", "content": "hack"},
                            )
                        ]
                    )
                if step == 3:
                    # 3) 确认权限拦截消息并返回正常写 + 并发 search_tools 验证无锁不阻塞
                    has_perm_deny = any(
                        isinstance(m, ModelRequest)
                        for m in messages
                        if any(
                            isinstance(p, ToolReturnPart)
                            and p.outcome == "retried"
                            and "forbid" in str(p.content)
                            for p in m.parts
                        )
                    )
                    self.assertTrue(has_perm_deny)
                    return ModelResponse(
                        parts=[
                            ToolCallPart(tool_name="write_file", args={"path": "ok.py", "content": "1"}),
                            ToolCallPart(tool_name="search_tools", args={"queries": ["read_file"]}),
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="guarded-and-queued")])

            agent = Agent(
                FunctionModel(model_fn),
                deps_type=object,
                toolsets=[ts],
                capabilities=sort_capabilities([perm, defer_cap, queue, _SearchSpy(), search_cap]),
            )
            res = await agent.run("测试激活后的权限与队列", workspace=LocalWorkspaceBackend(ws))
            self.assertEqual(res.output, "guarded-and-queued")
            self.assertEqual(executed_writes, ["ok.py"])
            self.assertTrue(search_saw_write_active)

    async def test_role_spec_integration_and_coexistence_with_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            skill_dir = ws / ".agent" / "skills" / "deploy-guide"
            skill_dir.mkdir(parents=True)
            _ = (skill_dir / "SKILL.md").write_text(
                "---\nname: deploy-guide\ndescription: Deployment checklist\n---\nRun ext_deploy.\n",
                encoding="utf-8",
            )

            ext_ts: FunctionToolset[object] = FunctionToolset()

            @ext_ts.tool_plain
            def read_status() -> str:
                """Read current service status."""
                return "healthy"

            @ext_ts.tool_plain
            def ext_deploy(env: str) -> str:
                """部署服务到目标环境。"""
                return f"deployed:{env}"

            _ = (read_status, ext_deploy)

            role = RoleSpec(
                name="deployer",
                description="负责发布部署",
                instructions="按规范执行发布。",
                model=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="unused")])),
                toolsets=[ext_ts],
                tool_disclosure=ToolDisclosurePolicy(
                    defer_patterns=("ext_*",),
                    strategy=bilingual_tool_search_fn,
                ),
            )

            turns: list[tuple[list[str], str | None]] = []

            def role_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = messages
                turns.append(([t.name for t in info.function_tools], info.instructions))
                if len(turns) == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(tool_name="load_capability", args={"id": "deploy-guide"}),
                            ToolCallPart(tool_name="search_tools", args={"queries": ["部署服务"]}),
                        ]
                    )
                if len(turns) == 2:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="ext_deploy", args={"env": "staging"})]
                    )
                return ModelResponse(parts=[TextPart(content="staging-ok")])

            skills_cap = build_skills_capability(global_dirs=(), builtin_dirs=())
            perm = PermissionGuardHook(reviewer=PermissionReviewer(), workspace_root=ws)
            queue = ToolExecutionQueueHook(workspace_root=ws)

            built_role = RoleSpec(
                name=role.name,
                description=role.description,
                instructions=role.instructions,
                model=FunctionModel(role_model),
                toolsets=role.toolsets,
                tool_disclosure=role.tool_disclosure,
            )
            agent = built_role.build_agent(extra_capabilities=[perm, queue, skills_cap])
            res = await agent.run("部署到 staging", workspace=LocalWorkspaceBackend(ws))
            self.assertEqual(res.output, "staging-ok")

            # Turn 0 仅可见 read_status 与 load_capability，ext_deploy 隐藏
            self.assertIn("read_status", turns[0][0])
            self.assertIn("load_capability", turns[0][0])
            self.assertNotIn("ext_deploy", turns[0][0])
            # DeferredCapabilityLoader 应感知 search_tools 表面并提示先 load capability
            assert turns[0][1] is not None
            self.assertIn("deploy-guide", turns[0][1])
            # Turn 1：ext_deploy 被 search_tools 激活
            self.assertIn("ext_deploy", turns[1][0])


if __name__ == "__main__":
    _ = unittest.main()
