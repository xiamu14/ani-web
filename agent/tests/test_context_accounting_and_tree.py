"""Step 12.4: Context Window 9 桶计量、真实 Usage 校准、Jev 辅助用量隔离与 Tree 双层读模型单测。"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast, override

import httpx
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from agent.context import ContextTreeManager
from agent.db import (
    CompressedFileMediaStore,
    SQLModelConversationStore,
    SQLModelProtocolStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)
from agent.llm import JevHttpClient
from agent.protocol import (
    ContextAccountingHook,
    RoleTaskUsageItemView,
    RuntimeEventBus,
    RunUsageView,
    build_tree_usage_summary,
)


class _AccountingFakeModel(Model):
    def __init__(self, *, input_tokens: int, cache_read_tokens: int = 0) -> None:
        super().__init__()
        self._input_tokens: int = input_tokens
        self._cache_read_tokens: int = cache_read_tokens

    @property
    @override
    def model_name(self) -> str:
        return "gpt-4o"

    @property
    @override
    def system(self) -> str:
        return "openai"

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        _ = (messages, model_settings, model_request_parameters)
        return ModelResponse(
            parts=[TextPart(content="calibrated-ok")],
            usage=RequestUsage(
                input_tokens=self._input_tokens,
                cache_read_tokens=self._cache_read_tokens,
                output_tokens=120,
            ),
        )


class TestStep124ContextAccountingAndTree(unittest.IsolatedAsyncioTestCase):
    """Step 12.4: 9 桶分桶估算、包含式 Usage 校准、Jev 用量隔离与 Tree 双层读模型。"""

    async def test_9_bucket_classification_and_inclusive_usage_calibration(self) -> None:
        bus = RuntimeEventBus(coalesce_window_sec=0.0)
        _sub, _ = bus.subscribe(conversation_id="conv-acc")
        saved_snaps: list[str] = []

        async def _on_snap(snap: object) -> None:
            saved_snaps.append(str(getattr(snap, "run_id", "")))

        hook = ContextAccountingHook(
            conversation_id="conv-acc",
            model_name="gpt-4o",
            event_bus=bus,
            known_mcp_prefixes=("github",),
            on_snapshot=_on_snap,
        )

        # 构造包含全部 8 类可见内容的上下文
        history: list[ModelMessage] = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="Global AGENTS.md rules for repository."),
                    SystemPromptPart(content="[Workspace Active: /tmp/ws]\nLocal AGENTS rules."),
                    SystemPromptPart(content="## [L0 Open WorkState]\nTask A in progress."),
                    UserPromptPart(
                        content=[
                            "Inspect [[attachment:att_1234567890ab | arch.png]] and [Externalized Historical Media (image/png, media+sha256://abc) — omitted]",
                            BinaryContent(data=b"png-bytes", media_type="image/png"),
                        ]
                    ),
                ]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="github_search_issues",
                        args='{"q":"bug"}',
                        tool_call_id="c1",
                    ),
                    ToolCallPart(
                        tool_name="memory_search",
                        args='{"query":"parser"}',
                        tool_call_id="c2",
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="github_search_issues",
                        content="Issue #42 found",
                        tool_call_id="c1",
                    ),
                    ToolReturnPart(
                        tool_name="memory_search",
                        content="Observation #7: parser fixed",
                        tool_call_id="c2",
                    ),
                ]
            ),
        ]

        # 1. 上游返回 input_tokens=10000, cache_read_tokens=6000 (大于估算总和 -> 正向 overhead)
        fake_model = _AccountingFakeModel(input_tokens=10_000, cache_read_tokens=6_000)
        agent = Agent(
            fake_model,
            capabilities=[hook],
            tools=[],
        )

        @agent.tool_plain
        def read_file(path: str) -> str:
            return path

        @agent.tool_plain(
            name="github_list_prs",
            metadata={"is_mcp_tool": True},
        )
        def github_list_prs(repo: str) -> str:
            return repo

        _ = await agent.run("总结问题", message_history=history)

        snap = hook.last_snapshot
        assert snap is not None
        self.assertTrue(snap.calibrated)
        # 红线断言: window_used_tokens 必须严格等于 input_tokens (10000)，绝不重复加 cache_read (6000)
        self.assertEqual(snap.window_used_tokens, 10_000)
        self.assertEqual(snap.uncached_input_tokens, 4_000)
        self.assertAlmostEqual(snap.cache_hit_ratio, 0.6, places=3)
        self.assertEqual(sum(b.tokens for b in snap.categories), 10_000)

        by_cat = {b.category: b.tokens for b in snap.categories}
        self.assertGreater(by_cat["system_prompt"], 0)
        self.assertGreater(by_cat["environment_info"], 0)
        self.assertGreater(by_cat["mcp"], 0)
        self.assertGreater(by_cat["builtin_tools"], 0)
        self.assertGreater(by_cat["memory"], 0)
        self.assertGreater(by_cat["attachments"], 0)
        self.assertGreater(by_cat["messages"], 0)
        self.assertGreater(by_cat["overhead"], 0)
        self.assertEqual(snap.savings.opt6_omitted_media_count, 1)
        self.assertEqual(len(saved_snaps), 1)

        # 验证 InstructionPart skill: 前缀归类与缩减比例校准 (actual < est)
        req_ctx = ModelRequestContext(
            model=fake_model,
            messages=history,
            model_settings=None,
            model_request_parameters=ModelRequestParameters(
                instruction_parts=[
                    InstructionPart(content="Skill L2 instructions " * 40, name="skill:coder-guide")
                ],
                function_tools=[
                    ToolDefinition(name="github_deferred", description="x", defer_loading=True, metadata={"is_mcp_tool": True})
                ],
            ),
        )
        from agent.protocol import estimate_context_buckets

        acc, _ = estimate_context_buckets(req_ctx, model_name="gpt-4o", mcp_prefixes=("github_",))
        self.assertGreater(acc.totals["skills"], 0)
        self.assertEqual(acc.savings.deferred_mcp_tools_count, 1)
        self.assertGreater(acc.savings.deferred_mcp_tokens_saved_est, 0)

        # 验证负向校准 (actual_input=20 < est_non_overhead) 时 sum(buckets) == 20
        hook_small = ContextAccountingHook(conversation_id="conv-small", model_name="gpt-4o")
        agent_small = Agent(
            _AccountingFakeModel(input_tokens=20, cache_read_tokens=0),
            capabilities=[hook_small],
        )
        _ = await agent_small.run("hi", message_history=history)
        snap_small = hook_small.last_snapshot
        assert snap_small is not None
        self.assertEqual(snap_small.window_used_tokens, 20)
        self.assertEqual(sum(b.tokens for b in snap_small.categories), 20)

        bus.aclose()

    async def test_jev_aux_usage_isolation_and_tree_two_level_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ctx.db"
            media_dir = Path(tmp) / "media"
            engine = create_sqlite_async_engine(db_path)
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            media_store = CompressedFileMediaStore(media_dir)
            store = SQLModelConversationStore(
                engine, session_factory=sessions, media_store=media_store
            )
            pstore = SQLModelProtocolStore(sessions)
            tree = ContextTreeManager(store)
            _ = await tree.bootstrap()
            root_conv = await tree.ensure_active_conversation(target_title="主分支")

            # 写入 2 轮消息（含一个大于阈值外置附件）
            big_img = BinaryContent(data=b"X" * (70 * 1024), media_type="image/png", identifier="att_big001")
            root_conv.messages.append(ModelRequest(parts=[UserPromptPart(content=["看图", big_img])]))
            root_conv.messages.append(ModelResponse(parts=[TextPart(content="收到大图")]))
            _ = await store.save(root_conv, scope="main")

            # 派生 handoff 分支（stop_backtrack=1）
            handoff_conv = await tree.fork_handoff(
                root_conv.conversation_id,
                summary="阶段交接摘要",
                target_title="子分支",
            )
            handoff_conv.messages.append(ModelRequest(parts=[UserPromptPart(content="继续子任务")]))
            _ = await store.save(handoff_conv, scope="main")

            # 测试 Jev on_usage 回调与辅助用量记录
            recorded_aux: list[Mapping[str, object]] = []

            async def _on_jev_usage(u: Mapping[str, object]) -> None:
                recorded_aux.append(u)
                await pstore.record_aux_usage(
                    conversation_id=handoff_conv.conversation_id,
                    source="semantic_router",
                    input_tokens=int(cast(int, u.get("prompt_tokens", 0))),
                    output_tokens=int(cast(int, u.get("completion_tokens", 0))),
                    total_tokens=int(cast(int, u.get("total_tokens", 0))),
                )

            def _jev_handler(_req: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "model": "typesafe/jev-1.14",
                        "answers": {"action": {"choice": "CONTINUE"}},
                        "usage": {"prompt_tokens": 120, "completion_tokens": 15, "total_tokens": 135},
                    },
                )

            jev = JevHttpClient(
                api_key="test-key",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(_jev_handler)),
                on_usage=_on_jev_usage,
            )
            _ = await jev.request_decisions(state={"x": 1}, questions={"action": {"type": "choice"}})
            await jev.aclose()
            self.assertEqual(len(recorded_aux), 1)

            aux_rows = await pstore.list_aux_usage(handoff_conv.conversation_id)
            summary = build_tree_usage_summary(
                conversation_id=handoff_conv.conversation_id,
                main_usage=RunUsageView(input_tokens=500, output_tokens=100, total_tokens=600, requests=1),
                role_items=[
                    RoleTaskUsageItemView(
                        task_id="T1",
                        role_name="coder",
                        usage=RunUsageView(input_tokens=1000, output_tokens=200, total_tokens=1200, requests=2),
                    )
                ],
                aux_rows=aux_rows,
            )
            self.assertEqual(summary.main_router_usage.total_tokens, 600)
            self.assertEqual(summary.role_tasks_total.total_tokens, 1200)
            self.assertEqual(len(summary.auxiliary_jev_usage), 1)
            self.assertEqual(summary.auxiliary_jev_usage[0].total_tokens, 135)

            # 测试 Level 1 Tree 拓扑
            topo = await pstore.read_tree_topology(include_role_branches=True)
            self.assertIsNotNone(topo.root_entry_id)
            self.assertEqual(len(topo.branches), 2)

            # 测试 Level 2 Entry 分页（handoff 分支遇 stop_backtrack=1 截断，主分支含 OPT-6 附件引用）
            handoff_page = await pstore.read_entry_page(handoff_conv.conversation_id, limit=20)
            self.assertTrue(any(e.kind == "handoff" and e.stop_backtrack for e in handoff_page.entries))
            self.assertFalse(any("收到大图" in (p.text or "") for e in handoff_page.entries for p in e.parts))

            root_page = await pstore.read_entry_page(root_conv.conversation_id, limit=20)
            att_parts = [p for e in root_page.entries for p in e.parts if p.attachment_id == "att_big001"]
            self.assertEqual(len(att_parts), 1)
            self.assertEqual(att_parts[0].content_url, "/v1/attachments/att_big001/content")

            # 测试 ContextSnapshot 持久化读写
            await pstore.save_context_snapshot(
                conversation_id=handoff_conv.conversation_id,
                run_id="run-snap-1",
                model_name="gpt-4o",
                window_used_tokens=10000,
                window_limit_tokens=128000,
                calibrated=True,
                snapshot_json=json.dumps({"ok": True}),
            )
            snap_row = await pstore.get_context_snapshot(handoff_conv.conversation_id)
            assert snap_row is not None
            self.assertEqual(snap_row.window_used_tokens, 10000)

            await engine.dispose()
