"""Step 7.1 单测: 行锚点漂移/失配校验、两级去重与 idfVeto、ACT-R 强化排序、60/30/10 截断与 L0/L1 渲染。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agent.memory import (
    SECONDS_PER_DAY,
    LineAnchor,
    ObservationRecord,
    SessionSummaryRecord,
    WorkStateEntryRecord,
    build_idf_lookup,
    classify_near_duplicate,
    extract_line_anchors_from_content,
    fold_work_state_list,
    format_line_anchor_suffix,
    rank_by_strength,
    render_l0_l1_context,
    tokenize_ws,
    truncate_field_60_30_10,
    verify_line_anchors,
)


class TestLineAnchors(unittest.TestCase):
    def test_extract_and_relocate_after_200_line_insertion_or_deletion(self) -> None:
        original_code = (
            "from __future__ import annotations\n"
            "\n"
            "def calculate_lithium_surcharge(weight_kg: float) -> float:\n"
            "    min_charge = 22.40\n"
            "    per_kg_rate = 3.15\n"
            "    return max(min_charge, weight_kg * per_kg_rate)\n"
            "\n"
            "def quote_carrier_rate(carrier: str, weight_kg: float) -> float:\n"
            "    base_fee = 12.00\n"
            "    return base_fee + calculate_lithium_surcharge(weight_kg)"
        )
        anchors = extract_line_anchors_from_content(
            "./src/shipping/tariffs.py",
            original_code,
            edited_snippet="def calculate_lithium_surcharge(weight_kg: float) -> float:\n    min_charge = 22.40",
        )
        self.assertEqual(len(anchors), 2)
        self.assertEqual(anchors[0].path, "src/shipping/tariffs.py")
        self.assertEqual(anchors[0].line, 3)
        self.assertEqual(anchors[1].line, 4)

        # 1. 在文件顶部插入 200 行注释 + 缩进改变 -> 仍应 100% 精确重定位到 L203, L204
        inserted_prefix = "\n".join(f"# header comment line {i}" for i in range(200))
        shifted_code = (
            inserted_prefix
            + "\n"
            + original_code.replace("    min_charge = 22.40", "        min_charge = 22.40")
        )
        ver_shifted = verify_line_anchors(anchors, "src/shipping/tariffs.py", shifted_code)
        self.assertIsNotNone(ver_shifted)
        assert ver_shifted is not None
        self.assertEqual(ver_shifted.state, "found")
        self.assertEqual(ver_shifted.lines, (203, 204))
        self.assertEqual(format_line_anchor_suffix(ver_shifted), " @L203,L204")

        # 2. 修改了 min_charge = 24.75 -> 变成 partly-changed (@L203 (some code changed since))
        modified_value_code = shifted_code.replace("min_charge = 22.40", "min_charge = 24.75")
        ver_partial = verify_line_anchors(anchors, "src/shipping/tariffs.py", modified_value_code)
        assert ver_partial is not None
        self.assertEqual(ver_partial.state, "partly-changed")
        self.assertEqual(ver_partial.lines, (203,))
        self.assertEqual(ver_partial.missing, 1)
        self.assertEqual(
            format_line_anchor_suffix(ver_partial),
            " @L203 (some code changed since)",
        )

        # 3. 目标函数被重构删除 -> 必须返回 changed，绝不猜测行号
        deleted_code = "def completely_new_pricing_engine() -> None:\n    pass\n"
        ver_deleted = verify_line_anchors(anchors, "src/shipping/tariffs.py", deleted_code)
        assert ver_deleted is not None
        self.assertEqual(ver_deleted.state, "changed")
        self.assertEqual(ver_deleted.lines, ())
        self.assertEqual(format_line_anchor_suffix(ver_deleted), " (code changed since)")


class TestNearDuplicateAndIdfVeto(unittest.TestCase):
    def test_exact_merge_candidate_and_rare_token_idf_veto(self) -> None:
        corpus_titles = [
            "Fix OAuth token persistence in MCP loader configuration",
            "Update OAuth token persistence for MCP server authentication",
            "Add SQLite schema migration for observations table",
            "Implement ToolLockCoordinator shared read lock for MCP tools",
            "Configure Logfire instrumentation for SuperAgentRuntime",
            "Handle timeout error in JevHttpClient decisions request",
        ]
        idf_fn = build_idf_lookup([tokenize_ws(t) for t in corpus_titles])

        # 1. Tier-0 Exact: 大小写/标点差异但归一化与标识符完全一致
        exact_res = classify_near_duplicate(
            "Fix `build_decisions_url` /v1 suffix stripping!",
            "fix build_decisions_url /v1 suffix stripping",
            idf_fn,
        )
        self.assertEqual(exact_res.tier, "exact")

        # 2. 不同代码标识符（如 FileTreeStore vs DiskStore）绝不合并
        code_id_diff = classify_near_duplicate(
            "Use FileTreeStore for OAuth token persistence in MCP",
            "Use DiskStore for OAuth token persistence in MCP",
            idf_fn,
        )
        self.assertEqual(code_id_diff.tier, "none")

        # 3. 共享高频词但含高 IDF 稀有区分词（如 404 vs 401）触发 idfVeto 否决合并
        rare_token_diff = classify_near_duplicate(
            "Fix OAuth token persistence in MCP loader configuration",
            "Fix OAuth token persistence in MCP loader configuration for Knowra401Error",
            idf_fn,
            cosine_threshold=0.60,
            veto_theta_idf=2.0,
        )
        self.assertEqual(rare_token_diff.tier, "none")

        # 4. 同义重排且无稀有冲突词 -> Tier-1 candidate
        reworded = classify_near_duplicate(
            "OAuth token persistence in MCP loader configuration",
            "MCP loader configuration for OAuth token persistence",
            idf_fn,
            cosine_threshold=0.70,
            veto_theta_idf=3.5,
        )
        self.assertIn(reworded.tier, ("exact", "candidate"))


class TestActRRanking(unittest.TestCase):
    def test_recency_head_protected_and_reinforced_old_memory_climbs(self) -> None:
        now = 1_800_000_000  # 基准秒级时间戳

        def _make_obs(
            obs_id: int,
            days_ago: int,
            reinforcements_days_ago: tuple[int, ...] = (),
        ) -> ObservationRecord:
            created = now - days_ago * SECONDS_PER_DAY
            r_dates = tuple(
                datetime.fromtimestamp(now - d * SECONDS_PER_DAY, tz=UTC).strftime("%Y-%m-%d")
                for d in reinforcements_days_ago
            )
            return ObservationRecord(
                id=obs_id,
                project_key="proj",
                conversation_id="c1",
                role_name="coder",
                type="decision",
                title=f"obs-{obs_id}",
                created_at_epoch=created,
                reinforcement_dates=r_dates,
            )

        # 构造 6 条候选，目标选出 Top-4（recency_head_size(4) == 1，故 #6 最新必保）
        obs_newest = _make_obs(6, days_ago=0)
        obs_recent_noise_5 = _make_obs(5, days_ago=2)
        obs_recent_noise_4 = _make_obs(4, days_ago=3)
        obs_recent_noise_3 = _make_obs(3, days_ago=4)
        obs_old_unreinforced = _make_obs(2, days_ago=25)
        # #1 虽然创建于 30 天前，但在最近 3 天连续被强化印证
        obs_old_reinforced = _make_obs(
            1,
            days_ago=30,
            reinforcements_days_ago=(1, 2, 3, 5),
        )

        pool = [
            obs_newest,
            obs_recent_noise_5,
            obs_recent_noise_4,
            obs_recent_noise_3,
            obs_old_unreinforced,
            obs_old_reinforced,
        ]
        selected = rank_by_strength(pool, count=4, alpha=1.5, now_epoch=now)
        selected_ids = [o.id for o in selected]

        # 最新头部 #6 必在首位，且被多次强化的老核心知识 #1 必须击败未强化的中间噪音进入窗口
        self.assertEqual(selected_ids[0], 6)
        self.assertIn(1, selected_ids)
        self.assertNotIn(2, selected_ids)


class TestBudgetAndRendering(unittest.TestCase):
    def test_truncate_60_30_10_and_redaction(self) -> None:
        raw = (
            "HEADER_START "
            + "<private>top-secret-note</private> "
            + "api_key = 'sk-1234567890abcdef123456' "
            + ("A" * 5000)
            + " TAIL_ERROR_STACK"
        )
        truncated = truncate_field_60_30_10(raw, max_chars=400)
        self.assertIn("HEADER_START", truncated)
        self.assertIn("TAIL_ERROR_STACK", truncated)
        self.assertIn("<elided chars=", truncated)
        self.assertNotIn("top-secret-note", truncated)
        self.assertNotIn("sk-1234567890abcdef123456", truncated)
        self.assertIn("[REDACTED_PRIVATE]", truncated)
        self.assertIn("[REDACTED_SECRET]", truncated)

    def test_work_state_folding_and_l0_l1_stale_tag_rendering(self) -> None:
        entries = [
            WorkStateEntryRecord(
                id=1,
                project_key="p",
                list_name="sprint",
                task_name="step-7.1",
                status="in_progress",
                fields={"owner": "coder", "temp": "old"},
                created_at_epoch=100,
            ),
            WorkStateEntryRecord(
                id=2,
                project_key="p",
                list_name="sprint",
                task_name="step-7.1",
                status="in_progress",
                fields={"temp": None, "progress": "90%"},
                created_at_epoch=200,
            ),
            WorkStateEntryRecord(
                id=3,
                project_key="p",
                list_name="sprint",
                task_name="step-6.5",
                status="done",
                fields={"owner": "coder"},
                created_at_epoch=150,
            ),
        ]
        folded = fold_work_state_list(entries)
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0].task_name, "step-7.1")
        self.assertEqual(dict(folded[0].fields), {"owner": "coder", "progress": "90%"})

        obs_fresh = ObservationRecord(
            id=10,
            project_key="p",
            conversation_id="c",
            role_name="coder",
            type="decision",
            title="Use FileTreeV1KeySanitizationStrategy for URL keys",
            facts=("Prevents slashes in URL keys from creating nested directories",),
            created_at_epoch=1_700_000_100,
        )
        obs_stale = ObservationRecord(
            id=9,
            project_key="p",
            conversation_id="c",
            role_name="coder",
            type="discovery",
            title="Lithium battery minimum charge is 22.40",
            line_anchors=(LineAnchor(path="tariffs.py", line=10, text="min_charge = 22.40"),),
            created_at_epoch=1_700_000_000,
        )
        ver_stale = verify_line_anchors(
            obs_stale.line_anchors,
            "tariffs.py",
            "min_charge = 24.75\n",
        )
        assert ver_stale is not None
        rendered = render_l0_l1_context(
            observations=[obs_fresh, obs_stale],
            summaries=[
                SessionSummaryRecord(
                    id=1,
                    project_key="p",
                    conversation_id="c",
                    task_id="T1",
                    request="Implement MCP & Memory",
                    completed="Steps 6.3-6.5",
                    learned="Sanitize FileTreeStore keys",
                    next_steps="Step 7.2 SQLite FTS5",
                    created_at_epoch=1_700_000_200,
                )
            ],
            work_states=entries,
            anchor_verifications={9: ver_stale},
        )
        self.assertIn("Open Work State (L0)", rendered)
        self.assertIn("step-7.1 (owner=coder, progress=90%)", rendered)
        self.assertNotIn("step-6.5", rendered)
        self.assertIn("Latest Session Summary (L0)", rendered)
        self.assertIn("[STALE - CODE CHANGED] Lithium battery minimum charge is 22.40 (code changed since)", rendered)
        self.assertIn("#10 ", rendered)
