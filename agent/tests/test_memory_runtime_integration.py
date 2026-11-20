"""Step 7.4 集成测试: open_runtime 装配、Role 自动沉淀与跨任务 L0+L1 注入、full_fork 免重复注入与 Handoff 状态恢复。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.core.roles import RoleRegistry, RoleSpec
from agent.core.runtime import open_runtime
from agent.memory import build_l0_l1_memory_block, derive_project_key


class TestMemoryRuntimeIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_role_archives_memory_and_next_role_receives_l0_l1_and_l3_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "workspace"
            ws.mkdir()
            target_file = ws / "pricing.py"
            _ = target_file.write_text(
                "def compute_surcharge(weight: float) -> float:\n"
                + "    min_charge = 22.40\n"
                + "    return max(min_charge, weight * 3.15)\n",
                encoding="utf-8",
            )

            coder_calls = 0
            researcher_system_prompts: list[str] = []
            researcher_tool_names: list[str] = []

            def _coder_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal coder_calls
                _ = messages
                coder_calls += 1
                if coder_calls == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="save_memory",
                                args={
                                    "title": "Lithium surcharge minimum is defined in pricing.py",
                                    "facts": ["compute_surcharge enforces min_charge floor"],
                                    "type": "discovery",
                                    "files_modified": ["pricing.py"],
                                },
                            )
                        ]
                    )
                return ModelResponse(
                    parts=[TextPart(content="Verified pricing.py surcharge implementation.")]
                )

            def _researcher_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                for msg in messages:
                    if isinstance(msg, ModelRequest):
                        for part in msg.parts:
                            if isinstance(part, SystemPromptPart):
                                researcher_system_prompts.append(part.content)
                        if msg.instructions:
                            researcher_system_prompts.append(msg.instructions)
                researcher_tool_names.extend(t.name for t in info.function_tools)
                return ModelResponse(parts=[TextPart(content="Researcher inspected memory.")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="You are coder.",
                        model=FunctionModel(_coder_model),
                        default_lock_mode="exclusive",
                    ),
                    RoleSpec(
                        name="researcher",
                        description="researcher",
                        instructions="You are researcher.",
                        model=FunctionModel(_researcher_model),
                        default_lock_mode="shared",
                    ),
                ]
            )

            async with open_runtime(
                db_path=tmp_path / "ctx.db",
                step_db_path=tmp_path / "steps.db",
                role_registry=registry,
                global_mcp_path=tmp_path / "no_global_mcp.json",
                mcp_workspace_root=ws,
            ) as runtime:
                main_conv = await runtime.context_tree.ensure_active_conversation(work_path=ws)
                proj_key = derive_project_key(ws)

                # 写入一条开放 WorkState
                _ = await runtime.memory_store.append_work_state(
                    project_key=proj_key,
                    list_name="roadmap",
                    task_name="audit-pricing",
                    status="in_progress",
                    fields={"owner": "researcher"},
                )

                # 1. 启动 coder 任务：调用 save_memory 并在任务完成时自动归档 Trajectory Observation + Summary
                r1 = await runtime.scheduler.dispatch_task(
                    main_conversation_id=main_conv.conversation_id,
                    role_name="coder",
                    mandate="Inspect pricing.py and record surcharge memory",
                    work_path=str(ws),
                )
                await runtime.scheduler.wait_all()
                t1 = await runtime.ticket_store.get_ticket(main_conv.conversation_id, r1.task_id)
                assert t1 is not None
                self.assertEqual(t1.status, "completed")

                stored_obs = await runtime.memory_store.list_recent_observations(project_key=proj_key)
                stored_summaries = await runtime.memory_store.list_recent_summaries(project_key=proj_key)
                self.assertGreaterEqual(len(stored_obs), 1)
                self.assertEqual(len(stored_summaries), 1)

                # 2. full_fork 分支模式应跳过 L0+L1 注入（返回空字符串）
                fork_block = await build_l0_l1_memory_block(
                    runtime.memory_store,
                    work_path=ws,
                    branch_mode="full_fork",
                )
                self.assertEqual(fork_block, "")

                # 3. 启动 researcher 任务：启动时自动获注 L0+L1 记忆块，并挂载 L3 MemoryToolset 4 个工具
                r2 = await runtime.scheduler.dispatch_task(
                    main_conversation_id=main_conv.conversation_id,
                    role_name="researcher",
                    mandate="Check prior pricing memory",
                    work_path=str(ws),
                )
                await runtime.scheduler.wait_all()
                t2 = await runtime.ticket_store.get_ticket(main_conv.conversation_id, r2.task_id)
                assert t2 is not None
                self.assertEqual(t2.status, "completed")

                combined_prompts = "\n".join(researcher_system_prompts)
                self.assertIn("Workspace Memory (L0 State & L1 Timeline Index)", combined_prompts)
                self.assertIn("audit-pricing", combined_prompts)
                self.assertIn("Lithium surcharge minimum is defined in pricing.py", combined_prompts)
                for tool_name in ("memory_search", "memory_timeline", "get_observations", "save_memory"):
                    self.assertIn(tool_name, researcher_tool_names)

                # 4. Handoff / Compaction 后新主分支自动附带 L0 状态
                handoff_conv = await runtime.context_tree.fork_handoff(
                    main_conv.conversation_id,
                    summary="Phase 1 pricing check done",
                    work_path=ws,
                )
                handoff_texts = [
                    part.content
                    for msg in handoff_conv.messages
                    if isinstance(msg, ModelRequest)
                    for part in msg.parts
                    if isinstance(part, SystemPromptPart)
                ]
                merged_handoff = "\n".join(handoff_texts)
                self.assertIn("Open Work State (L0)", merged_handoff)
                self.assertIn("audit-pricing", merged_handoff)
                self.assertIn("Latest Session Summary (L0)", merged_handoff)
