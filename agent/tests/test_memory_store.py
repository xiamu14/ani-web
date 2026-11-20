"""Step 7.2 单测: SQLModelMemoryStore (SQLite + FTS5)、Tier-0 强化合并、特异性文件检索、Timeline 与 Supersede。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.db import SQLModelMemoryStore, create_sqlite_async_engine, init_async_db
from agent.memory import (
    SECONDS_PER_DAY,
    CreateObservationInput,
    CreateSessionSummaryInput,
    LineAnchor,
    fold_work_state_list,
)


class TestSQLModelMemoryStore(unittest.IsolatedAsyncioTestCase):
    async def test_tier0_merge_reinforcement_and_fts5_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_sqlite_async_engine(Path(tmp) / "memory.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            day1 = 1_750_000_000
            day2 = day1 + 2 * SECONDS_PER_DAY

            res1 = await store.save_observation(
                CreateObservationInput(
                    project_key="ani-agent",
                    conversation_id="c1",
                    role_name="coder",
                    type="bugfix",
                    title="Strip /v1 suffix in build_decisions_url for OpenRouter Jev",
                    facts=("OpenRouter /alpha/decisions returns 404 when /v1 is kept",),
                    concepts=("gotcha", "problem-solution"),
                    files_read=("src/agent/llm/jev_client.py",),
                    files_modified=("./src/agent/llm/jev_client.py",),
                    line_anchors=(
                        LineAnchor(
                            path="src/agent/llm/jev_client.py",
                            line=14,
                            text='cleaned = base_url.rstrip("/").removesuffix("/v1")',
                        ),
                    ),
                    discovery_tokens=1200,
                    created_at_epoch=day1,
                )
            )
            self.assertFalse(res1.merged_into_existing)
            self.assertEqual(len(res1.record.reinforcement_dates), 1)

            # 跨天写入归一化同名 Observation -> Tier-0 自动合并，表行数不增加，追加强化日期与 facts
            res2 = await store.save_observation(
                CreateObservationInput(
                    project_key="ani-agent",
                    conversation_id="c2",
                    role_name="researcher",
                    type="bugfix",
                    title="strip /v1 suffix in `build_decisions_url` for OpenRouter Jev!",
                    facts=("Verified with Knowra MCP E2E run",),
                    concepts=("how-it-works",),
                    files_read=("tests/e2e_knowra_mcp.py",),
                    created_at_epoch=day2,
                )
            )
            self.assertTrue(res2.merged_into_existing)
            self.assertEqual(res2.record.id, res1.record.id)
            self.assertEqual(len(res2.record.reinforcement_dates), 2)
            self.assertEqual(len(res2.record.facts), 2)
            self.assertIn("tests/e2e_knowra_mcp.py", res2.record.files_read)

            all_rows = await store.list_recent_observations(project_key="ani-agent")
            self.assertEqual(len(all_rows), 1)

            # FTS5 BM25 全文检索（命中 facts 中的 Knowra 或 title 中的 build_decisions_url）
            fts_hits = await store.search_observations(
                project_key="ani-agent",
                query="Knowra build_decisions_url",
                concept_filter="gotcha",
            )
            self.assertEqual([h.id for h in fts_hits], [res1.record.id])

            await engine.dispose()

    async def test_file_specificity_timeline_and_supersede(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_sqlite_async_engine(Path(tmp) / "memory2.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            base_epoch = 1_760_000_000
            # #1 宽泛只读观察（读了 5 个文件，未修改 target.py）
            obs1 = (
                await store.save_observation(
                    CreateObservationInput(
                        project_key="proj",
                        conversation_id="c1",
                        role_name="researcher",
                        type="discovery",
                        title="Survey storage modules across project",
                        files_read=("a.py", "b.py", "c.py", "d.py", "src/target.py"),
                        created_at_epoch=base_epoch + 10,
                    )
                )
            ).record
            # #2 旧决策：使用 DiskStore 修改了 src/target.py
            obs2 = (
                await store.save_observation(
                    CreateObservationInput(
                        project_key="proj",
                        conversation_id="c1",
                        role_name="coder",
                        type="decision",
                        title="Use DiskStore for OAuth token persistence",
                        files_modified=("src/target.py",),
                        created_at_epoch=base_epoch + 20,
                    )
                )
            ).record
            # #3 新决策：改用 FileTreeStore 修改了 src/target.py
            obs3 = (
                await store.save_observation(
                    CreateObservationInput(
                        project_key="proj",
                        conversation_id="c1",
                        role_name="coder",
                        type="decision",
                        title="Use FileTreeStore with key sanitization for OAuth token persistence",
                        files_modified=("src/target.py",),
                        created_at_epoch=base_epoch + 30,
                    )
                )
            ).record
            # #4 后续测试验证
            obs4 = (
                await store.save_observation(
                    CreateObservationInput(
                        project_key="proj",
                        conversation_id="c1",
                        role_name="tester",
                        type="change",
                        title="Verify Knowra OAuth E2E passes",
                        files_read=("tests/e2e_knowra_mcp.py",),
                        created_at_epoch=base_epoch + 40,
                    )
                )
            ).record

            # 1. 文件特异性排序：修改了 src/target.py 且聚焦单文件的 #3, #2 排在宽泛只读的 #1 前面
            file_obs = await store.list_observations_for_file(
                project_key="proj",
                file_path="./src/target.py",
            )
            self.assertEqual([o.id for o in file_obs], [obs3.id, obs2.id, obs1.id])

            # 2. Timeline 邻域查询：以 #2 为锚点，前 1 后 1 -> [#1, #2, #3]
            timeline = await store.get_timeline(
                project_key="proj",
                anchor_id=obs2.id,
                depth_before=1,
                depth_after=1,
            )
            self.assertEqual([o.id for o in timeline], [obs1.id, obs2.id, obs3.id])

            # 3. 标记 #2 被 #3 supersede 后，默认列表与文件查询自动隐藏旧决策 #2
            await store.mark_superseded(
                project_key="proj",
                observation_ids=[obs2.id],
                superseded_by=obs3.id,
            )
            active_file_obs = await store.list_observations_for_file(
                project_key="proj",
                file_path="src/target.py",
            )
            self.assertEqual([o.id for o in active_file_obs], [obs3.id, obs1.id])

            fetched = await store.get_observations_by_ids(
                project_key="proj",
                ids=[obs4.id, obs2.id],
            )
            self.assertEqual([o.id for o in fetched], [obs4.id, obs2.id])
            self.assertEqual(fetched[1].superseded_by, obs3.id)

            await engine.dispose()

    async def test_summaries_and_work_state_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_sqlite_async_engine(Path(tmp) / "memory3.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            summary = await store.save_summary(
                CreateSessionSummaryInput(
                    project_key="proj",
                    conversation_id="c1",
                    task_id="T1",
                    request="Implement Step 7.2",
                    completed="SQLModelMemoryStore with FTS5",
                    learned="Triggers keep observations_fts synchronized",
                    next_steps="Step 7.3 hooks and toolset",
                    files_edited=("./src/agent/db/memory_store.py",),
                    created_at_epoch=1000,
                )
            )
            self.assertEqual(summary.files_edited, ("src/agent/db/memory_store.py",))
            summaries = await store.list_recent_summaries(project_key="proj")
            self.assertEqual(len(summaries), 1)

            _ = await store.append_work_state(
                project_key="proj",
                list_name="plan",
                task_name="step-7.1",
                status="done",
                fields={"owner": "coder"},
                created_at_epoch=100,
            )
            _ = await store.append_work_state(
                project_key="proj",
                list_name="plan",
                task_name="step-7.2",
                status="in_progress",
                fields={"owner": "coder", "temp": "1"},
                created_at_epoch=200,
            )
            _ = await store.append_work_state(
                project_key="proj",
                list_name="plan",
                task_name="step-7.2",
                status="in_progress",
                fields={"temp": None, "db": "fts5"},
                created_at_epoch=300,
            )

            raw_states = await store.list_work_states(project_key="proj")
            folded = fold_work_state_list(raw_states)
            self.assertEqual(len(folded), 1)
            self.assertEqual(folded[0].task_name, "step-7.2")
            self.assertEqual(dict(folded[0].fields), {"owner": "coder", "db": "fts5"})

            await engine.dispose()
