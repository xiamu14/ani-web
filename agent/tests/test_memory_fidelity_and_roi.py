"""Step 7.5: 双轴评测基准（轴一：抗毒化正确性 Fidelity + 轴二：减少重复工作与净 Token ROI）。

覆盖 docs/1120-memory-spec.md 第四节定义的 8 个确定性评测场景（F1–F4 + E1–E4）
以及 Arm OFF vs Arm ON-Fresh vs Arm ON-Stale 三臂对照指标。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, RunContext
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
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from agent.db import SQLModelMemoryStore, create_sqlite_async_engine, init_async_db
from agent.hooks import FileMemoryContextHook
from agent.memory import (
    CreateObservationInput,
    LineAnchor,
    RoleMemoryBundle,
    archive_completed_role_task,
    build_idf_lookup,
    classify_near_duplicate,
    derive_project_key,
    extract_line_anchors_from_content,
    format_line_anchor_suffix,
    prepare_role_memory_bundle,
    tokenize_ws,
    verify_line_anchors,
)


def _dummy_ctx() -> RunContext[object]:
    return RunContext(deps=object(), model=TestModel(), usage=RunUsage(), run_id="run-mem-75")


@dataclass(frozen=True)
class ArmMetrics:
    """单臂运行的量化评测指标。"""

    arm_name: str
    full_file_reads: int
    targeted_window_reads: int
    repo_grep_calls: int
    failed_trial_calls: int
    prompt_chars: int
    final_answer: str

    @property
    def estimated_tokens(self) -> int:
        return max(1, self.prompt_chars // 4)


def _flatten_messages_text(messages: list[ModelMessage]) -> str:
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            if msg.instructions:
                parts.append(msg.instructions)
            for p in msg.parts:
                if isinstance(p, (UserPromptPart, ToolReturnPart)):
                    parts.append(str(p.content))
    return "\n".join(parts)


class TestAxis1MemoryFidelityAndAntiPoisoning(unittest.IsolatedAsyncioTestCase):
    """轴一：正确记忆与抗毒化评测（Fidelity & Anti-Poisoning，目标 100% 通过）。"""

    async def test_case_f1_stale_value_trap_override_rate_100_percent(self) -> None:
        """Case F1: 过期数值诱饵陷阱（Stale-Value Trap）— Arm ON-Stale 100% 核验真实代码，拒绝输出 22.40。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_sqlite_async_engine(root / "f1.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            billing = root / "billing.py"
            lines = [f"# padding line {i}" for i in range(1, 520)]
            lines.append("def compute_min_charge() -> float:")
            lines.append("    min_charge = 24.75  # updated fee")
            lines.append("    return min_charge")
            _ = billing.write_text("\n".join(lines) + "\n", encoding="utf-8")

            project_key = derive_project_key(str(root))
            # 写入一条过期诱饵记忆：声称 min_charge = 22.40，且锚点文本还是旧版 22.40
            _ = await store.save_observation(
                CreateObservationInput(
                    project_key=project_key,
                    conversation_id="conv_seed",
                    role_name="coder",
                    type="change",
                    title="Set min_charge default to 22.40 in billing.py",
                    facts=("min_charge was set to 22.40 in compute_min_charge",),
                    files_modified=("billing.py",),
                    line_anchors=(
                        LineAnchor(
                            path="billing.py",
                            line=521,
                            text="    min_charge = 22.40",
                        ),
                    ),
                )
            )

            bundle = await prepare_role_memory_bundle(
                store,
                work_path=str(root),
                conversation_id="conv_f1",
                role_name="coder",
                branch_mode="root_only",
            )
            # L1 注入必须自动标记该记忆为 [STALE - CODE CHANGED] 与 (code changed since)
            self.assertIn("[STALE - CODE CHANGED]", bundle.extra_instructions)
            self.assertIn("(code changed since)", bundle.extra_instructions)

            def stale_aware_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                tool_returns = [
                    p
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, ToolReturnPart)
                ]
                if not tool_returns:
                    instr = "\n".join(
                        m.instructions or "" for m in messages if isinstance(m, ModelRequest)
                    )
                    assert "[STALE - CODE CHANGED]" in instr
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="read_file",
                                args={"path": "billing.py", "offset": 519, "limit": 6},
                            )
                        ]
                    )
                code_window = str(tool_returns[-1].content)
                if "24.75" in code_window:
                    return ModelResponse(parts=[TextPart(content="verified_min_charge=24.75")])
                return ModelResponse(parts=[TextPart(content="poisoned_min_charge=22.40")])

            agent: Agent[None, str] = Agent(
                FunctionModel(stale_aware_model),
                instructions=bundle.extra_instructions,
                capabilities=list(bundle.capabilities),
                toolsets=list(bundle.toolsets),
            )

            @agent.tool_plain
            def read_file(path: str, offset: int = 1, limit: int = 50) -> str:
                file_lines = (root / path).read_text(encoding="utf-8").splitlines()
                window = file_lines[max(0, offset - 1) : offset - 1 + limit]
                return "\n".join(window)

            res = await agent.run("What is the exact min_charge in billing.py?")
            self.assertEqual(res.output, "verified_min_charge=24.75")
            self.assertNotIn("22.40", res.output)
            await engine.dispose()

    def test_case_f2_anchor_drift_and_deletion_100_percent_accuracy(self) -> None:
        """Case F2: 行号大幅漂移（+200 行）与代码删除陷阱 — 100% 精确重定位或标记 changed，零猜测。"""
        base_lines = [f"# header {i}" for i in range(1, 100)]
        base_lines.extend(
            [
                "class RateLimiter:",
                "    def check_quota(self, tenant_id: str) -> bool:",
                "        return tenant_id.startswith('pro_')",
            ]
        )
        base_content = "\n".join(base_lines) + "\n"
        anchors = extract_line_anchors_from_content(
            "limiter.py",
            base_content,
            edited_snippet="def check_quota(self, tenant_id: str) -> bool:",
        )
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0].line, 101)

        # 1. 上方插入 200 行无关代码 -> 必须 100% 重定位到 101 + 200 = 301 行
        inserted = [f"# inserted line {i}" for i in range(200)]
        drifted_content = "\n".join(inserted + base_lines) + "\n"
        ver_drifted = verify_line_anchors(anchors, "limiter.py", drifted_content)
        assert ver_drifted is not None
        self.assertEqual(ver_drifted.state, "found")
        self.assertEqual(ver_drifted.lines, (301,))
        self.assertEqual(format_line_anchor_suffix(ver_drifted), " @L301")

        # 2. 目标函数被删除 -> 必须 100% 标记为 changed 且 lines 为空元组
        deleted_content = "\n".join(inserted) + "\nclass EmptyLimiter:\n    pass\n"
        ver_deleted = verify_line_anchors(anchors, "limiter.py", deleted_content)
        assert ver_deleted is not None
        self.assertEqual(ver_deleted.state, "changed")
        self.assertEqual(ver_deleted.lines, ())
        self.assertEqual(format_line_anchor_suffix(ver_deleted), " (code changed since)")

    async def test_case_f3_contradiction_supersede_and_idf_veto_precision(self) -> None:
        """Case F3: 架构决策推翻覆盖（Supersede）与稀有词冲突否决（idfVeto）100% 准确。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_sqlite_async_engine(root / "f3.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            auth_py = root / "auth.py"
            _ = auth_py.write_text(
                "class TokenVerifier:\n    ALGORITHM = 'RS256'\n",
                encoding="utf-8",
            )
            project_key = derive_project_key(str(root))

            # 旧决策 #1：使用 HS256
            old_saved = await store.save_observation(
                CreateObservationInput(
                    project_key=project_key,
                    conversation_id="conv_1",
                    role_name="architect",
                    type="decision",
                    title="Configure TokenVerifier JWT signing algorithm in auth.py",
                    facts=("TokenVerifier uses symmetric HS256 algorithm",),
                    files_modified=("auth.py",),
                    line_anchors=(
                        LineAnchor(
                            path="auth.py",
                            line=2,
                            text="    ALGORITHM = 'HS256'",
                        ),
                    ),
                )
            )
            # 新决策 #2：通过 FileMemoryContextHook 写穿失效旧决策并记录 RS256
            hook = FileMemoryContextHook(
                store=store,
                project_key=project_key,
                workspace_root=root,
                conversation_id="conv_2",
                role_name="architect",
            )
            _ = await hook.after_tool_execute(
                _dummy_ctx(),
                call=ToolCallPart(
                    tool_name="edit_file",
                    args={"path": "auth.py", "replacement": "    ALGORITHM = 'RS256'"},
                ),
                tool_def=ToolDefinition(name="edit_file", description="edit"),
                args={"path": "auth.py", "replacement": "    ALGORITHM = 'RS256'"},
                result="edited",
            )

            # L1 活跃观察中旧决策 HS256 已被标记 superseded_by 并从活跃列表剔除
            active_obs = await store.list_recent_observations(
                project_key=project_key,
                include_superseded=False,
            )
            active_ids = [o.id for o in active_obs]
            self.assertNotIn(old_saved.record.id, active_ids)
            self.assertGreaterEqual(len(active_obs), 1)

            # 同时验证 idfVeto：标题高度相似但包含不同稀有错误码（ERR_OAUTH_PKCE_401 vs ERR_SQLITE_LOCK_503）绝不误合并
            title_a = "Fix intermittent production failure in gateway handler ERR_OAUTH_PKCE_401"
            title_b = "Fix intermittent production failure in gateway handler ERR_SQLITE_LOCK_503"
            corpus = [
                "Fix intermittent production failure in gateway handler request pipeline",
                "Update intermittent production failure in gateway handler retry policy",
                "Handle intermittent production failure in gateway handler middleware",
                title_a,
                title_b,
            ]
            idf_fn = build_idf_lookup([tokenize_ws(t) for t in corpus])
            verdict = classify_near_duplicate(
                title_a,
                title_b,
                idf_fn,
                cosine_threshold=0.60,
                veto_theta_idf=2.0,
            )
            self.assertEqual(verdict.tier, "none")
            await engine.dispose()

    async def test_case_f4_extraction_groundedness_zero_hallucination(self) -> None:
        """Case F4: 提取器事实精确率审计 — LLM 文本虚构的文件名/行号 0% 混入结构化字段。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_sqlite_async_engine(root / "f4.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            real_file = root / "real_service.py"
            _ = real_file.write_text(
                "class RealService:\n    def execute_order(self, order_id: str) -> bool:\n        return True\n",
                encoding="utf-8",
            )

            # 工具真实只修改了 real_service.py，但 LLM 回复文本中虚构声称修改了 phantom_hallucinated.py 的第 999 行
            messages: list[ModelMessage] = [
                ModelRequest(parts=[UserPromptPart(content="Update execute_order")]),
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="edit_file",
                            args={
                                "path": "real_service.py",
                                "old_string": "return False",
                                "replacement": "def execute_order(self, order_id: str) -> bool:",
                            },
                            tool_call_id="tc_real",
                        )
                    ]
                ),
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="edit_file",
                            content="Updated real_service.py successfully",
                            tool_call_id="tc_real",
                        )
                    ]
                ),
                ModelResponse(
                    parts=[
                        TextPart(
                            content="I also modified phantom_hallucinated.py at line 999 and secret_nonexistent.py at line 42."
                        )
                    ]
                ),
            ]

            await archive_completed_role_task(
                store,
                work_path=str(root),
                conversation_id="conv_f4",
                task_id="task_f4",
                role_name="coder",
                mandate="Update execute_order",
                output="Modified phantom_hallucinated.py at line 999",
                messages=messages,
                discovery_tokens=200,
            )

            saved = await store.list_recent_observations(
                project_key=derive_project_key(str(root)),
            )
            self.assertEqual(len(saved), 1)
            obs = saved[0]
            self.assertEqual(obs.files_modified, ("real_service.py",))
            self.assertNotIn("phantom_hallucinated.py", obs.files_modified)
            self.assertNotIn("phantom_hallucinated.py", obs.files_read)
            disk_lines = real_file.read_text(encoding="utf-8").splitlines()
            for anchor in obs.line_anchors:
                self.assertEqual(anchor.path, "real_service.py")
                self.assertEqual(disk_lines[anchor.line - 1].strip(), anchor.text.strip())
            await engine.dispose()


class TestAxis2EfficiencyAndNetTokenROI(unittest.IsolatedAsyncioTestCase):
    """轴二：减少重复工作与净 Token ROI 评测（Efficiency & ROI，覆盖 E1–E4）。"""

    async def test_case_e1_and_e3_large_file_and_multi_role_handoff_saves_over_50_percent(self) -> None:
        """Case E1 & E3: >32KB 大文件跨 Role 接力定位修改 — Arm ON-Fresh 相比 Arm OFF 整文件读取与全库搜索减少 >= 50%。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_sqlite_async_engine(root / "e1.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)

            large_file = root / "monolith_engine.py"
            lines: list[str] = []
            for idx in range(1, 901):
                if idx == 740:
                    lines.append("def calculate_settlement_adjustment(amount: float) -> float:")
                elif idx == 741:
                    lines.append("    adjustment_factor = 1.035  # critical settlement rule")
                elif idx == 742:
                    lines.append("    return round(amount * adjustment_factor, 4)")
                else:
                    lines.append(
                        f"def helper_func_{idx:04d}(x: int) -> int: return x + {idx}  # padding line to exceed 32KB threshold"
                    )
            full_text = "\n".join(lines) + "\n"
            _ = large_file.write_text(full_text, encoding="utf-8")
            self.assertGreater(large_file.stat().st_size, 32 * 1024)

            # Researcher Role 完成探索并归档记忆（定位到 monolith_engine.py @L740）
            research_msgs: list[ModelMessage] = [
                ModelRequest(parts=[UserPromptPart(content="Locate calculate_settlement_adjustment")]),
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="read_file",
                            args={"path": "monolith_engine.py", "offset": 738, "limit": 8},
                            tool_call_id="tc_r1",
                        )
                    ]
                ),
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="read_file",
                            content="\n".join(lines[737:745]),
                            tool_call_id="tc_r1",
                        )
                    ]
                ),
                ModelResponse(
                    parts=[
                        TextPart(
                            content="Located calculate_settlement_adjustment in monolith_engine.py with adjustment_factor = 1.035."
                        )
                    ]
                ),
            ]
            await archive_completed_role_task(
                store,
                work_path=str(root),
                conversation_id="conv_researcher",
                task_id="task_researcher",
                role_name="researcher",
                mandate="Locate calculate_settlement_adjustment in monolith_engine.py",
                output="Located calculate_settlement_adjustment in monolith_engine.py (adjustment_factor = 1.035)",
                messages=research_msgs,
                discovery_tokens=450,
            )

            async def run_coder_arm(*, enable_memory: bool) -> ArmMetrics:
                full_reads = 0
                window_reads = 0
                grep_calls = 0
                captured_prompts: list[str] = []

                if enable_memory:
                    bundle = await prepare_role_memory_bundle(
                        store,
                        work_path=str(root),
                        conversation_id="conv_coder_on",
                        role_name="coder",
                        branch_mode="root_only",
                    )
                else:
                    bundle = RoleMemoryBundle(extra_instructions="", capabilities=(), toolsets=())

                def coder_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                    captured_prompts.append(_flatten_messages_text(messages))
                    tool_returns = [
                        p
                        for m in messages
                        if isinstance(m, ModelRequest)
                        for p in m.parts
                        if isinstance(p, ToolReturnPart)
                    ]
                    instr = "\n".join(
                        m.instructions or "" for m in messages if isinstance(m, ModelRequest)
                    )
                    if not tool_returns:
                        if "monolith_engine.py" in instr and "@L" in instr:
                            return ModelResponse(
                                parts=[
                                    ToolCallPart(
                                        tool_name="read_file",
                                        args={"path": "monolith_engine.py", "offset": 738, "limit": 8},
                                    )
                                ]
                            )
                        return ModelResponse(
                            parts=[
                                ToolCallPart(
                                    tool_name="grep_repo",
                                    args={"pattern": "calculate_settlement_adjustment"},
                                ),
                                ToolCallPart(
                                    tool_name="read_file",
                                    args={"path": "monolith_engine.py", "offset": 1, "limit": 450},
                                ),
                                ToolCallPart(
                                    tool_name="read_file",
                                    args={"path": "monolith_engine.py", "offset": 451, "limit": 450},
                                ),
                            ]
                        )
                    return ModelResponse(parts=[TextPart(content="adjustment_factor=1.035")])

                agent: Agent[None, str] = Agent(
                    FunctionModel(coder_model),
                    instructions=bundle.extra_instructions,
                    capabilities=list(bundle.capabilities),
                    toolsets=list(bundle.toolsets),
                )

                @agent.tool_plain
                def grep_repo(pattern: str) -> str:
                    nonlocal grep_calls
                    grep_calls += 1
                    return f"monolith_engine.py: matched {pattern}"

                @agent.tool_plain
                def read_file(path: str, offset: int = 1, limit: int = 900) -> str:
                    nonlocal full_reads, window_reads
                    _ = path
                    if limit >= 400:
                        full_reads += 1
                    else:
                        window_reads += 1
                    return "\n".join(lines[max(0, offset - 1) : offset - 1 + limit])

                res = await agent.run("Verify adjustment_factor in calculate_settlement_adjustment")
                return ArmMetrics(
                    arm_name="ON-Fresh" if enable_memory else "OFF",
                    full_file_reads=full_reads,
                    targeted_window_reads=window_reads,
                    repo_grep_calls=grep_calls,
                    failed_trial_calls=0,
                    prompt_chars=sum(len(p) for p in captured_prompts),
                    final_answer=res.output,
                )

            arm_off = await run_coder_arm(enable_memory=False)
            arm_on = await run_coder_arm(enable_memory=True)

            self.assertEqual(arm_off.final_answer, "adjustment_factor=1.035")
            self.assertEqual(arm_on.final_answer, "adjustment_factor=1.035")
            self.assertEqual(arm_off.full_file_reads, 2)
            self.assertEqual(arm_on.full_file_reads, 0)
            self.assertEqual(arm_off.repo_grep_calls, 1)
            self.assertEqual(arm_on.repo_grep_calls, 0)
            self.assertEqual(arm_on.targeted_window_reads, 1)
            token_saving_ratio = 1.0 - (arm_on.estimated_tokens / arm_off.estimated_tokens)
            self.assertGreaterEqual(token_saving_ratio, 0.65)
            await engine.dispose()

    async def test_case_e2_gotcha_recall_zero_repeated_trial_and_error(self) -> None:
        """Case E2: 跨会话踩坑免疫（Gotcha Recall）— 已知踩坑在 Arm ON-Fresh 下失败试错次数降为 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_sqlite_async_engine(root / "e2.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)
            project_key = derive_project_key(str(root))

            _ = await store.save_observation(
                CreateObservationInput(
                    project_key=project_key,
                    conversation_id="conv_prev",
                    role_name="coder",
                    type="bugfix",
                    title="Gotcha: run_migration requires --lock-timeout=5000 flag",
                    facts=("Calling run_migration without --lock-timeout=5000 fails with LockAcquireError",),
                    concepts=("gotcha", "problem-solution"),
                    files_modified=("migrate.py",),
                )
            )

            async def run_migration_arm(*, enable_memory: bool) -> int:
                failed_attempts = 0
                if enable_memory:
                    bundle = await prepare_role_memory_bundle(
                        store,
                        work_path=str(root),
                        conversation_id="conv_e2_on",
                        role_name="coder",
                        branch_mode="root_only",
                    )
                    extra_instr = bundle.extra_instructions
                else:
                    extra_instr = ""

                def model_fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                    tool_returns = [
                        p
                        for m in messages
                        if isinstance(m, ModelRequest)
                        for p in m.parts
                        if isinstance(p, ToolReturnPart)
                    ]
                    instr = "\n".join(
                        m.instructions or "" for m in messages if isinstance(m, ModelRequest)
                    )
                    if not tool_returns:
                        flag = "--lock-timeout=5000" if "--lock-timeout=5000" in instr else ""
                        return ModelResponse(
                            parts=[
                                ToolCallPart(
                                    tool_name="run_migration",
                                    args={"flags": flag},
                                )
                            ]
                        )
                    last_ret = str(tool_returns[-1].content)
                    if "LockAcquireError" in last_ret:
                        return ModelResponse(
                            parts=[
                                ToolCallPart(
                                    tool_name="run_migration",
                                    args={"flags": "--lock-timeout=5000"},
                                )
                            ]
                        )
                    return ModelResponse(parts=[TextPart(content="migration_ok")])

                agent: Agent[None, str] = Agent(FunctionModel(model_fn), instructions=extra_instr)

                @agent.tool_plain
                def run_migration(flags: str) -> str:
                    nonlocal failed_attempts
                    if "--lock-timeout=5000" not in flags:
                        failed_attempts += 1
                        return "Error: LockAcquireError (hint: pass --lock-timeout=5000)"
                    return "Migration succeeded"

                res = await agent.run("Execute database migration")
                self.assertEqual(res.output, "migration_ok")
                return failed_attempts

            failures_off = await run_migration_arm(enable_memory=False)
            failures_on = await run_migration_arm(enable_memory=True)

            self.assertGreaterEqual(failures_off, 1)
            self.assertEqual(failures_on, 0)
            await engine.dispose()

    async def test_case_e4_small_file_break_even_zero_bloat(self) -> None:
        """Case E4: <1.5KB 小文件免打扰 Break-Even 校验 — 零额外 L2 时间线膨胀。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_sqlite_async_engine(root / "e4.db")
            await init_async_db(engine)
            store = SQLModelMemoryStore(engine)
            project_key = derive_project_key(str(root))

            small_file = root / "constants.py"
            small_content = "APP_VERSION = '1.0.0'\nMAX_RETRIES = 3\n"
            _ = small_file.write_text(small_content, encoding="utf-8")
            self.assertLess(small_file.stat().st_size, 1536)

            _ = await store.save_observation(
                CreateObservationInput(
                    project_key=project_key,
                    conversation_id="c1",
                    role_name="coder",
                    type="change",
                    title="Update MAX_RETRIES in constants.py",
                    facts=("Set MAX_RETRIES = 3",),
                    files_modified=("constants.py",),
                )
            )
            hook = FileMemoryContextHook(
                store=store,
                project_key=project_key,
                workspace_root=root,
                conversation_id="c2",
                role_name="coder",
            )
            out = await hook.after_tool_execute(
                _dummy_ctx(),
                call=ToolCallPart(tool_name="read_file", args={"path": "constants.py"}),
                tool_def=ToolDefinition(name="read_file", description="read"),
                args={"path": "constants.py"},
                result=small_content,
            )
            self.assertEqual(out, small_content)
            await engine.dispose()


if __name__ == "__main__":
    _ = unittest.main()
