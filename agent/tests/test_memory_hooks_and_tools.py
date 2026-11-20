"""Step 7.3 单测: FileMemoryContextHook (L2 文件唤醒/去重/写穿失效)、L3 三步检索工具集与轨迹提炼器。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from agent.db import SQLModelMemoryStore, create_sqlite_async_engine, init_async_db
from agent.hooks import FileMemoryContextHook
from agent.memory import (
    CreateObservationInput,
    LineAnchor,
    TrajectoryMemoryExtractor,
    build_memory_toolset,
)


def _dummy_ctx() -> RunContext[object]:
    return RunContext(deps=object(), model=TestModel(), usage=RunUsage(), run_id="run-mem-73")


class TestFileMemoryContextHook(unittest.IsolatedAsyncioTestCase):
    async def test_small_file_skip_large_file_hint_dedup_and_write_through_supersede(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            engine = create_sqlite_async_engine(ws / "mem.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            small_file = ws / "small.py"
            _ = small_file.write_text("x = 1\n", encoding="utf-8")

            medium_file = ws / "tariffs.py"
            padding = "\n".join(f"# padding comment line {i}" for i in range(80))
            medium_content = (
                f"{padding}\n"
                "def calculate_lithium_surcharge(weight_kg: float) -> float:\n"
                "    min_charge = 22.40\n"
                "    return max(min_charge, weight_kg * 3.15)\n"
            )
            _ = medium_file.write_text(medium_content, encoding="utf-8")

            # 为 small.py 与 tariffs.py 均预先写入一条带行锚点的观察
            _ = await store.save_observation(
                CreateObservationInput(
                    project_key="proj",
                    conversation_id="c1",
                    role_name="researcher",
                    type="discovery",
                    title="Small config file note",
                    files_read=("small.py",),
                    created_at_epoch=1000,
                )
            )
            seeded_tariff_obs = (
                await store.save_observation(
                    CreateObservationInput(
                        project_key="proj",
                        conversation_id="c1",
                        role_name="researcher",
                        type="discovery",
                        title="Lithium battery surcharge minimum charge is 22.40",
                        files_read=("tariffs.py",),
                        line_anchors=(
                            LineAnchor(
                                path="tariffs.py",
                                line=10,
                                text="    min_charge = 22.40",
                            ),
                        ),
                        created_at_epoch=2000,
                    )
                )
            ).record

            hook = FileMemoryContextHook(
                store=store,
                project_key="proj",
                workspace_root=ws,
                conversation_id="c2",
                role_name="coder",
            )
            ctx = _dummy_ctx()

            # 1. < 1.5KB 的小文件读取不注入任何时间线
            res_small = await hook.after_tool_execute(
                ctx,
                call=ToolCallPart(tool_name="read_file", args={"path": "small.py"}),
                tool_def=ToolDefinition(name="read_file", description="read"),
                args={"path": "small.py"},
                result="x = 1\n",
            )
            self.assertEqual(res_small, "x = 1\n")

            # 2. >= 1.5KB 文件首次读取：注入 L2 时间线，并实时重定位 min_charge = 22.40 到 @L82
            res_med_1 = await hook.after_tool_execute(
                ctx,
                call=ToolCallPart(tool_name="read_file", args={"path": "tariffs.py"}),
                tool_def=ToolDefinition(name="read_file", description="read"),
                args={"path": "tariffs.py"},
                result=medium_content,
            )
            self.assertIn("[L2 File Memory Timeline: tariffs.py]", str(res_med_1))
            self.assertIn("@L82", str(res_med_1))

            # 3. 同一会话第二次读取同一未变更文件：单次去重，不再重复追加
            res_med_2 = await hook.after_tool_execute(
                ctx,
                call=ToolCallPart(tool_name="read_file", args={"path": "tariffs.py"}),
                tool_def=ToolDefinition(name="read_file", description="read"),
                args={"path": "tariffs.py"},
                result=medium_content,
            )
            self.assertEqual(res_med_2, medium_content)

            # 4. 执行 edit_file 把 min_charge = 22.40 改为 24.75 -> 自动写穿失效旧观察并记录新观察
            updated_content = medium_content.replace("min_charge = 22.40", "min_charge = 24.75")
            _ = medium_file.write_text(updated_content, encoding="utf-8")
            _ = await hook.after_tool_execute(
                ctx,
                call=ToolCallPart(
                    tool_name="edit_file",
                    args={"path": "tariffs.py", "replacement": "    min_charge = 24.75"},
                ),
                tool_def=ToolDefinition(name="edit_file", description="edit"),
                args={"path": "tariffs.py", "replacement": "    min_charge = 24.75"},
                result="edited",
            )

            # 验证旧观察已被标记 superseded_by，活跃文件观察列表中仅剩最新修改观察
            active_for_file = await store.list_observations_for_file(
                project_key="proj",
                file_path="tariffs.py",
                include_superseded=False,
            )
            self.assertEqual(len(active_for_file), 1)
            self.assertNotEqual(active_for_file[0].id, seeded_tariff_obs.id)
            old_fetched = await store.get_observations_by_ids(
                project_key="proj",
                ids=[seeded_tariff_obs.id],
            )
            self.assertEqual(old_fetched[0].superseded_by, active_for_file[0].id)

            # 5. 验证 Step 8.2 & 8.3: FileMemoryContextHook 原生支持 get_instructions/get_toolset，
            # 且当同会话任务结束运行 TrajectoryMemoryExtractor 时，临时机械 "Modified tariffs.py via edit_file" 自动合流被任务级观察取代
            cap_hook = FileMemoryContextHook(
                store=store,
                project_key="proj",
                workspace_root=ws,
                conversation_id="c2",
                role_name="coder",
                l0_l1_instructions="=== [Memory L0/L1] ===",
                include_l3_toolset=True,
            )
            self.assertEqual(cap_hook.get_instructions(), "=== [Memory L0/L1] ===")
            self.assertIsNotNone(cap_hook.get_toolset())

            extractor = TrajectoryMemoryExtractor(store)
            task_obs, _ = await extractor.extract_and_store(
                project_key="proj",
                conversation_id="c2",
                task_id="t-tariff",
                role_name="coder",
                mandate="Raise lithium surcharge minimum charge to 24.75",
                output="Updated calculate_lithium_surcharge min_charge to 24.75 in tariffs.py",
                messages=[
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="edit_file",
                                args={"path": "tariffs.py", "replacement": "    min_charge = 24.75"},
                            )
                        ]
                    )
                ],
                workspace_root=ws,
            )
            assert task_obs is not None
            post_task_active = await store.list_observations_for_file(
                project_key="proj",
                file_path="tariffs.py",
                include_superseded=False,
            )
            self.assertEqual(len(post_task_active), 1)
            self.assertEqual(post_task_active[0].id, task_obs.record.id)

            await engine.dispose()


class TestL3MemoryToolsetAndExtractor(unittest.IsolatedAsyncioTestCase):
    async def test_three_step_progressive_recall_and_trajectory_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            engine = create_sqlite_async_engine(ws / "mem_l3.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            code_file = ws / "auth.py"
            _ = code_file.write_text(
                "def build_oauth_storage(ns_dir: str) -> object:\n"
                "    return FileTreeV1KeySanitizationStrategy(directory=ns_dir)\n",
                encoding="utf-8",
            )

            # 1. 测试 TrajectoryMemoryExtractor 从真实工具轨迹提取 Observation 与 Summary
            extractor = TrajectoryMemoryExtractor(store)
            messages: list[ModelMessage] = [
                ModelRequest(parts=[]),
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="edit_file",
                            args={
                                "path": "auth.py",
                                "replacement": "    return FileTreeV1KeySanitizationStrategy(directory=ns_dir)",
                            },
                            tool_call_id="tc-1",
                        )
                    ]
                ),
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="edit_file",
                            content="ok",
                            tool_call_id="tc-1",
                        )
                    ]
                ),
            ]
            saved_obs, summary = await extractor.extract_and_store(
                project_key="proj",
                conversation_id="conv-1",
                task_id="T1",
                role_name="coder",
                mandate="Fix FileTreeStore URL key sanitization",
                output="Applied FileTreeV1KeySanitizationStrategy in auth.py to sanitize URL keys.",
                messages=messages,
                workspace_root=ws,
                discovery_tokens=850,
            )
            self.assertIsNotNone(saved_obs)
            assert saved_obs is not None
            self.assertEqual(saved_obs.record.files_modified, ("auth.py",))
            self.assertEqual(len(saved_obs.record.line_anchors), 1)
            self.assertEqual(saved_obs.record.line_anchors[0].line, 2)
            self.assertEqual(summary.files_edited, ("auth.py",))

            # 2. 通过 Agent + build_memory_toolset 执行 L3 三步检索与 save_memory
            toolset = build_memory_toolset(
                store,
                project_key="proj",
                workspace_root=ws,
                conversation_id="conv-2",
                role_name="researcher",
            )
            obs_id = saved_obs.record.id
            step = 0
            tool_outputs: list[str] = []

            def _model_fn(msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal step
                for part in msgs[-1].parts:
                    if isinstance(part, ToolReturnPart):
                        tool_outputs.append(str(part.content))
                step += 1
                if step == 1:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="memory_search", args={"query": "FileTreeV1KeySanitizationStrategy"})]
                    )
                if step == 2:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="memory_timeline", args={"anchor_id": obs_id})]
                    )
                if step == 3:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="get_observations", args={"ids": [obs_id]})]
                    )
                if step == 4:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="save_memory",
                                args={
                                    "title": "Always pass FileTreeV1KeySanitizationStrategy to FileTreeStore",
                                    "facts": ["OAuth uses full URL as key; slashes break raw FileTreeStore"],
                                    "type": "decision",
                                    "files_modified": ["auth.py"],
                                },
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="ALL_STEPS_DONE")])

            agent = Agent(model=FunctionModel(_model_fn), toolsets=[toolset])
            run_res = await agent.run("Recall FileTreeStore fix and save rule")
            self.assertEqual(run_res.output, "ALL_STEPS_DONE")
            self.assertEqual(len(tool_outputs), 4)
            self.assertIn(f"#{obs_id}", tool_outputs[0])
            self.assertIn(f">>> #{obs_id}", tool_outputs[1])
            self.assertIn("Observation #", tool_outputs[2])
            self.assertIn("@L2", tool_outputs[2])
            self.assertIn("stored as new", tool_outputs[3])

            await engine.dispose()
