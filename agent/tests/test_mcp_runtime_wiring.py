"""Step 6.5 集成测试: open_runtime 预编译、Role 按需挂载 MCP、async with agent 懒连接与权限拦截。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.core.roles import RoleRegistry, RoleSpec
from agent.core.runtime import open_runtime
from agent.permissions import (
    ApprovalDecision,
    ApprovalItem,
    McpClassifiedDecision,
    ProbedMcpServer,
    ProbedMcpTool,
)

_MOCK_MCP_SERVER_CODE = """
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("mock-notes")

@mcp.tool()
def read_note(topic: str) -> str:
    \"\"\"Read a note by topic.\"\"\"
    return f"NOTE[{topic}]=hello-mcp"

@mcp.tool()
def purge_all() -> str:
    \"\"\"Dangerously purge all notes.\"\"\"
    return "PURGED"

if __name__ == "__main__":
    mcp.run()
"""


class _FakeApproval:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision:
        _ = user_intent
        self.calls.append(tuple(i.tool_name for i in items))
        return ApprovalDecision(approved=True)


class _DeterministicMcpClassifier:
    async def classify(
        self,
        server: ProbedMcpServer,
        tools: Sequence[ProbedMcpTool],
    ) -> list[McpClassifiedDecision]:
        _ = server
        decisions: list[McpClassifiedDecision] = []
        for tool in tools:
            if tool.name == "read_note":
                decisions.append(
                    McpClassifiedDecision(
                        tool_name=tool.name,
                        level="pass",
                        lock_mode="shared",
                        recommended_roles=("researcher",),
                        reason="safe read",
                    )
                )
            else:
                decisions.append(
                    McpClassifiedDecision(
                        tool_name=tool.name,
                        level="forbid",
                        lock_mode="exclusive",
                        recommended_roles=("coder",),
                        reason="destructive purge",
                    )
                )
        return decisions


class TestMcpRuntimeWiring(unittest.IsolatedAsyncioTestCase):
    async def test_open_runtime_compiles_and_role_lazy_connects_stdio_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "workspace"
            ws.mkdir()
            server_script = tmp_path / "mock_mcp_server.py"
            _ = server_script.write_text(_MOCK_MCP_SERVER_CODE, encoding="utf-8")

            ws_mcp = ws / ".agent" / "mcp.json"
            ws_mcp.parent.mkdir(parents=True)
            _ = ws_mcp.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "notes": {
                                "command": sys.executable,
                                "args": [str(server_script)],
                                "roles": ["researcher"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            seen_tool_returns: list[str] = []

            def _role_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                _ = info
                last = messages[-1]
                for part in last.parts:
                    if part.part_kind == "tool-return":
                        seen_tool_returns.append(str(getattr(part, "content", "")))
                if len(seen_tool_returns) == 0:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="notes_read_note", args={"topic": "spec"})]
                    )
                if len(seen_tool_returns) == 1:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="notes_purge_all", args={})]
                    )
                return ModelResponse(parts=[TextPart(content="DONE: " + " | ".join(seen_tool_returns))])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="researcher",
                        instructions="use mcp notes",
                        model=FunctionModel(_role_model),
                        default_lock_mode="shared",
                    )
                ]
            )
            approval = _FakeApproval()

            async with open_runtime(
                db_path=tmp_path / "ctx.db",
                step_db_path=tmp_path / "steps.db",
                role_registry=registry,
                approval_port=approval,
                global_mcp_path=tmp_path / "nonexistent_global_mcp.json",
                mcp_workspace_root=ws,
                mcp_token_root=tmp_path / "tokens",
                mcp_classifier=_DeterministicMcpClassifier(),
            ) as runtime:
                # 1. 启动完成增量预编译
                self.assertIsNotNone(runtime.mcp_compile_report)
                assert runtime.mcp_compile_report is not None
                self.assertEqual(runtime.mcp_compile_report.total_jev_calls, 1)
                self.assertEqual(len(runtime.mcp_compile_report.policies), 2)

                # 2. 调度 researcher 执行任务（触发 async with agent: 懒连接 Stdio MCP）
                main_conv = await runtime.context_tree.ensure_active_conversation()
                main_conv_id = main_conv.conversation_id
                receipt = await runtime.scheduler.dispatch_task(
                    main_conversation_id=main_conv_id,
                    role_name="researcher",
                    mandate="read spec note and try purge",
                    work_path=str(ws),
                )
                await runtime.scheduler.wait_all()
                ticket = await runtime.ticket_store.get_ticket(main_conv_id, receipt.task_id)
                self.assertIsNotNone(ticket)
                assert ticket is not None
                self.assertEqual(ticket.status, "completed")
                # notes_read_note 成功返回，notes_purge_all 被 forbid 拦截（不触发审批弹窗）
                self.assertIn("NOTE[spec]=hello-mcp", seen_tool_returns)
                self.assertEqual(approval.calls, [])
                self.assertIn("NOTE[spec]=hello-mcp", ticket.result_summary or "")
