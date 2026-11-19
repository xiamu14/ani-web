"""Step 5.4 单元测试: Agent Loop 内 Tools 任务队列（读写并发、写互斥、Shell 独占与先审后排）。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from agent.hooks import PermissionGuardHook, sort_capabilities
from agent.permissions import PermissionReviewer
from agent.tools import (
    ToolExecutionQueueHook,
    ToolLockCoordinator,
    resolve_tool_resource,
)


class TestToolQueueHook(unittest.IsolatedAsyncioTestCase):
    """验证单轮并发 ToolCallPart 的读写并发、写/Shell 串行与权限前置短路。"""

    def test_resolve_tool_resource_matches_table(self) -> None:
        ws = Path("/tmp/demo_ws").resolve()
        r_read = resolve_tool_resource("read_file", {"path": "a.py"}, workspace_root=ws)
        self.assertEqual((r_read.kind, r_read.lock_mode, r_read.key), ("path", "shared", str(ws / "a.py")))

        r_write = resolve_tool_resource("write_file", {"path": "a.py"}, workspace_root=ws)
        self.assertEqual((r_write.kind, r_write.lock_mode), ("path", "exclusive"))

        r_shell = resolve_tool_resource("shell", {"command": "pytest"}, workspace_root=ws)
        self.assertEqual((r_shell.kind, r_shell.lock_mode, r_shell.key), ("path", "exclusive", str(ws)))

        r_list = resolve_tool_resource("list_tasks", {}, workspace_root=ws, main_conversation_id="c1")
        self.assertEqual((r_list.kind, r_list.lock_mode), ("none", "none"))

        r_load = resolve_tool_resource("load_capability", {"id": "demo"}, workspace_root=ws)
        self.assertEqual((r_load.kind, r_load.lock_mode), ("none", "none"))

        r_cap_kind = resolve_tool_resource(
            "custom_loader", {}, workspace_root=ws, tool_kind="capability-load"
        )
        self.assertEqual((r_cap_kind.kind, r_cap_kind.lock_mode), ("none", "none"))

        r_search_kind = resolve_tool_resource(
            "custom_search", {}, workspace_root=ws, tool_kind="tool-search"
        )
        self.assertEqual((r_search_kind.kind, r_search_kind.lock_mode), ("none", "none"))

        r_steer = resolve_tool_resource(
            "steer_or_revise_task", {"task_id": "@T2"}, main_conversation_id="c1"
        )
        self.assertEqual((r_steer.kind, r_steer.key, r_steer.lock_mode), ("exact", "ticket:c1:T2", "exclusive"))

    async def test_shared_reads_run_concurrently_while_write_and_shell_serialize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()

            active_reads = 0
            max_concurrent_reads = 0
            active_writes = 0
            max_concurrent_writes = 0
            events: list[str] = []

            toolset: FunctionToolset[object] = FunctionToolset()

            @toolset.tool_plain
            async def read_file(path: str) -> str:
                nonlocal active_reads, max_concurrent_reads
                active_reads += 1
                max_concurrent_reads = max(max_concurrent_reads, active_reads)
                events.append(f"read_start:{path}")
                await asyncio.sleep(0.03)
                events.append(f"read_end:{path}")
                active_reads -= 1
                return f"ok:{path}"

            @toolset.tool_plain
            async def write_file(path: str, content: str) -> str:
                nonlocal active_writes, max_concurrent_writes
                _ = content
                active_writes += 1
                max_concurrent_writes = max(max_concurrent_writes, active_writes)
                events.append(f"write_start:{path}")
                await asyncio.sleep(0.02)
                events.append(f"write_end:{path}")
                active_writes -= 1
                return f"wrote:{path}"

            @toolset.tool_plain
            async def shell(command: str) -> str:
                nonlocal active_writes, max_concurrent_writes
                active_writes += 1
                max_concurrent_writes = max(max_concurrent_writes, active_writes)
                events.append(f"shell_start:{command}")
                await asyncio.sleep(0.02)
                events.append(f"shell_end:{command}")
                active_writes -= 1
                return f"ran:{command}"

            _ = (read_file, write_file, shell)

            def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = info
                has_return = any(
                    isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts)
                    for m in messages
                )
                if not has_return:
                    # 单轮同时发出 2 个读、1 个同文件写、1 个工作区 shell
                    return ModelResponse(
                        parts=[
                            ToolCallPart(tool_name="read_file", args={"path": "a.py"}),
                            ToolCallPart(tool_name="read_file", args={"path": "a.py"}),
                            ToolCallPart(tool_name="write_file", args={"path": "a.py", "content": "x"}),
                            ToolCallPart(tool_name="shell", args={"command": "python3 -V"}),
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="all done")])

            queue_hook = ToolExecutionQueueHook(workspace_root=ws)
            agent = Agent(
                model=FunctionModel(model_fn),
                deps_type=object,
                toolsets=[toolset],
                capabilities=sort_capabilities([queue_hook]),
            )
            res = await agent.run("并发读写测试")
            self.assertEqual(res.output, "all done")
            # 两个 read_file(a.py) 必须并发执行，而 write_file 与 shell 必须严格串行（互斥数为 1）
            self.assertEqual(max_concurrent_reads, 2)
            self.assertEqual(max_concurrent_writes, 1)
            self.assertLess(events.index("read_end:a.py"), events.index("write_start:a.py"))
            self.assertLess(events.index("write_end:a.py"), events.index("shell_start:python3 -V"))

    async def test_permission_denial_does_not_acquire_queue_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()

            toolset: FunctionToolset[object] = FunctionToolset()

            @toolset.tool_plain
            async def write_file(path: str, content: str) -> str:
                _ = (path, content)
                return "should_not_run"

            @toolset.tool_plain
            async def read_file(path: str) -> str:
                return f"read:{path}"

            _ = (write_file, read_file)

            def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = info
                has_return = any(
                    isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts)
                    for m in messages
                )
                if not has_return:
                    return ModelResponse(
                        parts=[
                            # 越界写将被 PermissionGuardHook 在进队列前直接拒绝
                            ToolCallPart(tool_name="write_file", args={"path": "/etc/passwd", "content": "x"}),
                            ToolCallPart(tool_name="read_file", args={"path": "ok.py"}),
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="handled")])

            perm = PermissionGuardHook(reviewer=PermissionReviewer(), workspace_root=ws)
            queue_hook = ToolExecutionQueueHook(workspace_root=ws)
            agent = Agent(
                model=FunctionModel(model_fn),
                deps_type=object,
                toolsets=[toolset],
                capabilities=sort_capabilities([queue_hook, perm]),
            )
            res = await agent.run("测试先审后排")
            self.assertEqual(res.output, "handled")

    async def test_shared_coordinator_serializes_writes_across_concurrent_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()

            shared_coord = ToolLockCoordinator()
            active_writes = 0
            max_concurrent_writes = 0
            active_reads = 0
            max_concurrent_reads = 0

            def build_worker_agent(op: str) -> Agent[object, str]:
                ts: FunctionToolset[object] = FunctionToolset()

                @ts.tool_plain
                async def read_file(path: str) -> str:
                    nonlocal active_reads, max_concurrent_reads
                    _ = path
                    active_reads += 1
                    max_concurrent_reads = max(max_concurrent_reads, active_reads)
                    await asyncio.sleep(0.03)
                    active_reads -= 1
                    return "ok"

                @ts.tool_plain
                async def write_file(path: str, content: str) -> str:
                    nonlocal active_writes, max_concurrent_writes
                    _ = (path, content)
                    active_writes += 1
                    max_concurrent_writes = max(max_concurrent_writes, active_writes)
                    await asyncio.sleep(0.03)
                    active_writes -= 1
                    return "wrote"

                _ = (read_file, write_file)

                def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                    _ = info
                    has_return = any(
                        isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts)
                        for m in messages
                    )
                    if not has_return:
                        if op == "read":
                            return ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "s.py"})])
                        return ModelResponse(
                            parts=[ToolCallPart(tool_name="write_file", args={"path": "s.py", "content": "1"})]
                        )
                    return ModelResponse(parts=[TextPart(content="done")])

                return Agent(
                    model=FunctionModel(model_fn),
                    deps_type=object,
                    toolsets=[ts],
                    capabilities=sort_capabilities(
                        [ToolExecutionQueueHook(workspace_root=ws, coordinator=shared_coord)]
                    ),
                )

            # 1) 两个独立 Agent 实例并发读同一个文件 -> 允许并发
            r1, r2 = await asyncio.gather(
                build_worker_agent("read").run("read 1"),
                build_worker_agent("read").run("read 2"),
            )
            self.assertEqual((r1.output, r2.output), ("done", "done"))
            self.assertEqual(max_concurrent_reads, 2)

            # 2) 两个独立 Agent 实例并发写同一个文件 -> 必须全局串行
            w1, w2 = await asyncio.gather(
                build_worker_agent("write").run("write 1"),
                build_worker_agent("write").run("write 2"),
            )
            self.assertEqual((w1.output, w2.output), ("done", "done"))
            self.assertEqual(max_concurrent_writes, 1)


if __name__ == "__main__":
    _ = unittest.main()
