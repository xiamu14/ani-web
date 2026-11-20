"""Step 11.1–11.4: 附件引用传输（Attachment）、内部产物快速引用（Artifact）、OPT-3/4/6 存储优化与严格溯源防伪测试。

包含全部 5 项审计修复的专项回归用例：
1. P0: CompressedFileMediaStore 与 AttachmentRegistry 共用存储目录对大文本 zlib 压缩/解压零冲突 + codec 准确性；
2. P1: SQLModelConversationStore.read_history() SQL 级 LIMIT 下推与 media_window 窗口化还原；
3. P1: register_attachment 工作区路径穿越拦截；
4. P2: citations.py #page&line 精确行范围核验 + 未知/畸形 fragment 拒绝；
5. P2: AttachmentRegistry.to_pydantic_ai_content() AudioUrl 映射。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai._uuid import uuid7
from pydantic_ai.conversation_store import ConversationEntry, ConversationRecord
from pydantic_ai.messages import (
    AudioUrl,
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextContent,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UploadedFile,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.media._store import MediaContext
from sqlmodel import col, select

from agent.attachments import (
    AttachmentRegistry,
    build_attachment_toolset,
    extract_and_verify_citations,
)
from agent.core import RoleRegistry, RoleSpec, open_runtime
from agent.db import (
    CompressedFileMediaStore,
    ContextEntryRow,
    SQLModelConversationStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)


class TestStep111AttachmentRegistryAndStrictCitations(unittest.TestCase):
    """Step 11.1: 附件去重、UploadedFile/AudioUrl 缓存、Artifact 读写与严格溯源防伪验证。"""

    def test_attachment_dedup_uploaded_file_audio_url_and_artifact_query(self) -> None:
        registry = AttachmentRegistry()

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"pixel_data_" * 200
        a1 = registry.register_bytes(
            png_bytes,
            title="dashboard.png",
            media_type="image/png",
            width=1920,
            height=1080,
        )
        a2 = registry.register_bytes(
            png_bytes,
            title="dup_dashboard.png",
            media_type="image/png",
        )
        self.assertEqual(a1.attachment_id, a2.attachment_id)
        self.assertIn("[[attachment:att_", a1.to_reference_handle())
        self.assertIn("1920x1080", a1.to_reference_handle())

        # 未绑定 UploadedFile 时返回 BinaryContent；绑定后返回 UploadedFile
        self.assertIsInstance(registry.to_pydantic_ai_content(a1.attachment_id), BinaryContent)
        _ = registry.bind_uploaded_file(
            a1.attachment_id,
            file_id="file-openai-998877",
            provider_name="openai",
        )
        c_up = registry.to_pydantic_ai_content(a1.attachment_id)
        self.assertIsInstance(c_up, UploadedFile)
        assert isinstance(c_up, UploadedFile)
        self.assertEqual(c_up.file_id, "file-openai-998877")

        # 审计修复 P2: 音频资产带 public_url 时必须返回 AudioUrl
        audio_ref = registry.register_bytes(
            b"ID3_fake_mp3_bytes",
            title="standup.mp3",
            media_type="audio/mpeg",
            duration_sec=95.0,
            public_url="https://cdn.example.com/audio/standup.mp3",
        )
        c_audio = registry.to_pydantic_ai_content(audio_ref.attachment_id)
        self.assertIsInstance(c_audio, AudioUrl)
        assert isinstance(c_audio, AudioUrl)
        self.assertEqual(c_audio.url, "https://cdn.example.com/audio/standup.mp3")

        # 内部 Artifact 快速 ID 注册与点号路径查询
        art = registry.save_artifact(
            "checkout_config",
            {"pricing": {"min_charge": 24.75, "currency": "USD"}, "tiers": ["free", "pro"]},
        )
        self.assertIn("[[artifact:checkout_config", art.to_reference_handle())
        self.assertEqual(
            registry.query_artifact_field("checkout_config", key_path="pricing.min_charge"),
            24.75,
        )
        self.assertEqual(
            registry.query_artifact_field("checkout_config", key_path="tiers.1"),
            "pro",
        )

    def test_strict_citation_line_range_and_malformed_fragment_rejection(self) -> None:
        registry = AttachmentRegistry()

        doc_text = (
            "Page 1 Header\nSystem Architecture Overview\f"
            "Line1: Pricing Rules Header\nLine2: Minimum lithium surcharge is 24.75 USD.\nLine3: Max weight 50kg."
        )
        doc_att = registry.register_bytes(
            doc_text.encode("utf-8"),
            title="tariff_spec.md",
            media_type="text/markdown",
        )
        vid_att = registry.register_bytes(
            b"fake_mp4_stream",
            title="oauth_keynote.mp4",
            media_type="video/mp4",
            duration_sec=120.0,
        )
        img_att = registry.register_bytes(
            b"\x89PNG\r\n\x1a\n_Fake_Diagram",
            title="latency_panel.png",
            media_type="image/png",
        )
        _ = registry.save_artifact("runtime_limits", {"concurrency": {"role_max": 4}})

        answer = (
            # 4 个合法溯源
            f"1. [[cite:{doc_att.attachment_id}#page=2&line=2-2 | \"Minimum lithium surcharge is 24.75 USD.\"]]\n"
            f"2. [[cite:{vid_att.attachment_id}#t=01:15-01:42 | \"OAuth 2.1 requires PKCE\"]]\n"
            f"3. [[cite:{img_att.attachment_id}#bbox=0.15,0.20,0.85,0.45 | \"P99 latency 420ms\"]]\n"
            "4. [[cite:art:runtime_limits#key=concurrency.role_max | \"4\"]]\n"
            # 审计修复 P2: 精确行区间不匹配（摘录在 line 2，却声称 line=1-1）-> 必须判 False
            f"5. [[cite:{doc_att.attachment_id}#page=2&line=1-1 | \"Minimum lithium surcharge is 24.75 USD.\"]]\n"
            # 审计修复 P2: 未知 locator key (#foo=bar) 与畸形片段 (#garbage) -> 必须判 False
            f"6. [[cite:{doc_att.attachment_id}#foo=bar | \"bad key\"]]\n"
            f"7. [[cite:{doc_att.attachment_id}#garbage | \"malformed\"]]\n"
            # 越界页码 & 越界时间戳 & 虚构 ID -> 必须判 False
            f"8. [[cite:{doc_att.attachment_id}#page=9 | \"oob page\"]]\n"
            f"9. [[cite:{vid_att.attachment_id}#t=02:30-03:00 | \"oob time\"]]\n"
            "10. [[cite:att_000000000000#page=1 | \"ghost\"]]\n"
        )

        citations = extract_and_verify_citations(answer, registry)
        self.assertEqual(len(citations), 10)

        for c in citations[:4]:
            self.assertTrue(c.verified, msg=f"Expected verified=True for {c.raw_tag}, got {c.verification_reason}")
            self.assertEqual(answer[c.anchor.start : c.anchor.end], c.raw_tag)

        # 5: 精确行区间拦截
        self.assertFalse(citations[4].verified)
        self.assertIn("lines 1-1", citations[4].verification_reason)
        # 6: 未知键拦截
        self.assertFalse(citations[5].verified)
        self.assertIn("Unknown locator key", citations[5].verification_reason)
        # 7: 畸形片段拦截
        self.assertFalse(citations[6].verified)
        self.assertIn("Malformed locator fragment", citations[6].verification_reason)
        # 8/9/10: 越界与虚构拦截
        self.assertFalse(citations[7].verified)
        self.assertFalse(citations[8].verified)
        self.assertFalse(citations[9].verified)


class TestStep112Opt3Opt4Opt6StorageAndSharedMediaStore(unittest.IsolatedAsyncioTestCase):
    """Step 11.2: OPT-3/4/6 存储、共享 CompressedFileMediaStore 零冲突解压、SQL LIMIT 下推与基准验证。"""

    async def test_shared_media_store_zlib_roundtrip_and_codec_accuracy(self) -> None:
        """审计修复 P0: CompressedFileMediaStore 压缩写入大文本后，共用同一 store 的 AttachmentRegistry 读取无乱码。"""
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "shared_blobs"
            media_store = CompressedFileMediaStore(media_dir, zlib_level=2)
            registry = AttachmentRegistry(media_store=media_store)

            # 1. 大文本先由 CompressedFileMediaStore 压缩落盘
            big_text = ("Log entry: request_id=abc123 status=200 latency=12ms\n" * 2000).encode("utf-8")
            uri = await media_store.put(big_text, context=MediaContext(media_type="text/plain"))
            meta = await media_store.get_metadata(uri)
            self.assertEqual(meta.get("codec"), "zlib")
            self.assertLess(media_store.blob_path_for_uri(uri).stat().st_size, len(big_text) // 4)

            # 2. AttachmentRegistry 注册同一份文本并读取原始字节 -> 必须自动解压还原，绝不返回 AGYZLIB1 乱码
            ref = registry.register_bytes(big_text, title="server.log", media_type="text/plain")
            loaded = registry.get_attachment_bytes(ref.attachment_id)
            self.assertEqual(loaded, big_text)

            # 3. 极短且无法压缩的文本 -> 回退原样直存且 metadata codec 精确标记为 "raw"
            tiny_text = b"hi"
            tiny_uri = await media_store.put(tiny_text, context=MediaContext(media_type="text/plain"))
            tiny_meta = await media_store.get_metadata(tiny_uri)
            self.assertEqual(tiny_meta.get("codec"), "raw")
            self.assertEqual(await media_store.get(tiny_uri), tiny_text)

    async def test_opt3_opt4_opt6_sql_limit_pushdown_and_window_hydration_benchmark(self) -> None:
        """验证 OPT-3 去重、OPT-4 zlib 压缩比、OPT-6 SQL LIMIT 下推与 media_window 窗口化还原性能。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = create_sqlite_async_engine(root / "conv.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            media_dir = root / "media_blobs"
            media_store = CompressedFileMediaStore(media_dir, zlib_level=2)

            store = SQLModelConversationStore(
                engine,
                session_factory=sessions,
                media_store=media_store,
                media_threshold_bytes=4096,
                media_window=2,
            )

            conv_rec = await store.create_conversation_from_root(conversation_id="conv-opt-346")
            head_id = conv_rec.head_id

            raw_png = b"\x89PNG\r\n\x1a\n" + bytes(i % 251 for i in range(100_000))
            large_log = "2026-11-20 INFO [worker-1] processing batch item...\n" * 2000

            entries: list[ConversationEntry] = []
            prev_id = head_id
            entry_ids: list[str] = []
            for idx in range(6):
                eid = str(uuid7())
                entry_ids.append(eid)
                if idx % 2 == 0:
                    msg: ModelMessage = ModelRequest(
                        parts=[
                            UserPromptPart(
                                content=[
                                    f"Turn {idx} screenshot",
                                    BinaryContent(
                                        data=raw_png,
                                        media_type="image/png",
                                        identifier=f"att_img_{idx}",
                                    ),
                                ]
                            )
                        ]
                    )
                else:
                    msg = ModelResponse(parts=[TextPart(content=large_log)])
                entries.append(ConversationEntry(id=eid, parent_id=prev_id, message=msg))
                prev_id = eid

            updated_rec = ConversationRecord(
                conversation_id=conv_rec.conversation_id,
                head_id=prev_id,
                scope=conv_rec.scope,
                revision=str(uuid7()),
            )
            await store.write_record(
                updated_rec,
                new_entries=entries,
                expected_revision=conv_rec.revision,
            )

            # 1. SQLite 体积缩减率 > 98%（6 条累计 >600KB 原始载荷，库内仅存 ~1KB 指针/行）
            async with sessions() as session:
                db_rows = (
                    await session.execute(
                        select(ContextEntryRow).where(col(ContextEntryRow.id).in_(entry_ids))
                    )
                ).scalars().all()
            total_db_json_bytes = sum(len((r.message_json or "").encode("utf-8")) for r in db_rows)
            raw_total_bytes = 3 * len(raw_png) + 3 * len(large_log.encode("utf-8"))
            self.assertLess(total_db_json_bytes / raw_total_bytes, 0.02)

            # 2. 磁盘 CAS 去重：6 条消息只有 2 个 .bin 文件（1 个原样 PNG + 1 个 zlib 压缩日志）
            bin_files = sorted(media_dir.glob("*.bin"))
            self.assertEqual(len(bin_files), 2)
            sizes = sorted(p.stat().st_size for p in bin_files)
            self.assertLess(sizes[0], 2048)
            self.assertEqual(sizes[1], len(raw_png))

            # 3. 审计修复 P1: SQL 级 LIMIT 下推 + media_window=2 窗口还原
            t0 = time.perf_counter()
            limited_history = await store.read_history(prev_id, limit=4, media_window=2)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertEqual(len(limited_history), 4)
            self.assertLess(elapsed_ms, 200.0)

            # 前 2 条在 media_window 外 -> 降级为紧凑占位符；后 2 条在 media_window 内 -> 100% 完整还原
            m0 = limited_history[0].message
            assert isinstance(m0, ModelRequest)
            p0 = m0.parts[0]
            assert isinstance(p0, UserPromptPart) and isinstance(p0.content, list)
            self.assertIsInstance(p0.content[1], TextContent)

            m1 = limited_history[1].message
            assert isinstance(m1, ModelResponse)
            tp1 = m1.parts[0]
            assert isinstance(tp1, TextPart)
            self.assertIn("Externalized Historical Text", tp1.content)

            m2 = limited_history[2].message
            assert isinstance(m2, ModelRequest)
            p2 = m2.parts[0]
            assert isinstance(p2, UserPromptPart) and isinstance(p2.content, list)
            self.assertIsInstance(p2.content[1], BinaryContent)
            assert isinstance(p2.content[1], BinaryContent)
            self.assertEqual(p2.content[1].data, raw_png)

            m3 = limited_history[3].message
            assert isinstance(m3, ModelResponse)
            tp3 = m3.parts[0]
            assert isinstance(tp3, TextPart)
            self.assertEqual(tp3.content, large_log)

            # 不存在的 head_id 依然抛出 LookupError
            with self.assertRaises(LookupError):
                _ = await store.read_history("nonexistent-entry-id", limit=2)

            await engine.dispose()


class TestStep113CrossRoleAttachmentsArtifactsAndSandboxE2E(unittest.IsolatedAsyncioTestCase):
    """Step 11.3: 工作区沙箱拦截 + open_runtime 下跨 Role 附件/产物引用传输与溯源核验 E2E。"""

    async def test_register_attachment_blocks_workspace_escape(self) -> None:
        """审计修复 P1: register_attachment 绑定 workspace_root 时拒绝逃逸路径（../../）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspace"
            ws.mkdir()
            outside = root / "secret.txt"
            _ = outside.write_text("TOP_SECRET", encoding="utf-8")

            registry = AttachmentRegistry()
            toolset = build_attachment_toolset(registry, workspace_root=ws)
            tool_out = ""

            def escape_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal tool_out
                returns = [
                    str(p.content)
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, ToolReturnPart)
                ]
                if not returns:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="register_attachment",
                                args={"file_path": "../secret.txt"},
                            )
                        ]
                    )
                tool_out = returns[-1]
                return ModelResponse(parts=[TextPart(content=tool_out)])

            agent: Agent[object, str] = Agent(
                FunctionModel(escape_model), deps_type=object, toolsets=[toolset]
            )
            _ = await agent.run("Register secret", deps=object())
            self.assertIn("access denied", tool_out)
            self.assertEqual(registry.list_attachments(), [])

    async def test_cross_role_attachment_and_artifact_handoff_with_verified_citations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspace"
            ws.mkdir()

            spec_doc = ws / "billing_spec.md"
            _ = spec_doc.write_text(
                "Page 1: General Overview\nStandard SLA is 99.9%.\f"
                + "Page 2: Surcharge Rules\nLithium battery minimum charge is 24.75 USD.\n",
                encoding="utf-8",
            )
            diagram_png = ws / "arch_panel.png"
            _ = diagram_png.write_bytes(b"\x89PNG\r\n\x1a\n_Fake_Arch_Diagram_Bytes")

            registered_doc_id = ""
            registered_img_id = ""

            def researcher_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal registered_doc_id, registered_img_id
                tool_returns = [
                    str(p.content)
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, ToolReturnPart)
                ]
                if not tool_returns:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="register_attachment",
                                args={"file_path": "billing_spec.md", "title": "billing_spec.md"},
                            ),
                            ToolCallPart(
                                tool_name="register_attachment",
                                args={
                                    "file_path": "arch_panel.png",
                                    "title": "arch_panel.png",
                                    "width": 1280,
                                    "height": 720,
                                },
                            ),
                            ToolCallPart(
                                tool_name="save_artifact",
                                args={
                                    "artifact_id": "surcharge_config",
                                    "json_value": '{"lithium": {"min_charge": 24.75, "unit": "USD"}}',
                                    "description": "Parsed surcharge config",
                                },
                            ),
                        ]
                    )
                for ret in tool_returns:
                    if "billing_spec.md" in ret and "[[attachment:" in ret:
                        registered_doc_id = ret.split("[[attachment:")[1].split("|")[0].strip()
                    if "arch_panel.png" in ret and "[[attachment:" in ret:
                        registered_img_id = ret.split("[[attachment:")[1].split("|")[0].strip()
                return ModelResponse(
                    parts=[
                        TextPart(
                            content=(
                                f"Registered {registered_doc_id}, {registered_img_id}, "
                                + "and art:surcharge_config [TASK_STATUS: COMPLETED]"
                            )
                        )
                    ]
                )

            coder_verified_report = ""

            def coder_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal coder_verified_report
                tool_returns = [
                    str(p.content)
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, ToolReturnPart)
                ]
                if not tool_returns:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="inspect_attachment",
                                args={"attachment_id": registered_doc_id, "page": 2},
                            ),
                            ToolCallPart(
                                tool_name="inspect_attachment",
                                args={"attachment_id": registered_img_id, "load_binary": True},
                            ),
                            ToolCallPart(
                                tool_name="get_artifact",
                                args={"artifact_id": "surcharge_config", "key_path": "lithium.min_charge"},
                            ),
                        ]
                    )
                draft_answer = (
                    f"Verified minimum charge from spec page 2 [[cite:{registered_doc_id}#page=2&line=2-2 | \"Lithium battery minimum charge is 24.75 USD.\"]], "
                    f"architecture panel region [[cite:{registered_img_id}#bbox=0.10,0.20,0.90,0.80 | \"Arch Panel\"]], "
                    "and internal artifact [[cite:art:surcharge_config#key=lithium.min_charge | \"24.75\"]]."
                )
                if len(tool_returns) == 3:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="verify_citations",
                                args={"draft_text": draft_answer},
                            )
                        ]
                    )
                coder_verified_report = tool_returns[-1]
                return ModelResponse(parts=[TextPart(content=f"{draft_answer} [TASK_STATUS: COMPLETED]")])

            registry = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="researcher",
                        instructions="Register attachments and artifacts",
                        model=FunctionModel(researcher_model),
                        default_lock_mode="shared",
                    ),
                    RoleSpec(
                        name="coder",
                        description="coder",
                        instructions="Inspect attachments/artifacts by ID and cite sources",
                        model=FunctionModel(coder_model),
                        default_lock_mode="exclusive",
                    ),
                ]
            )

            async with open_runtime(
                db_path=root / "ctx.db",
                step_db_path=root / "steps.db",
                role_registry=registry,
                global_mcp_path=root / "no_mcp.json",
                mcp_workspace_root=ws,
            ) as rt:
                conv = await rt.context_tree.ensure_active_conversation(work_path=ws)

                r1 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="researcher",
                    mandate="Register billing_spec.md, arch_panel.png, and surcharge_config",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t1 = await rt.ticket_store.get_ticket(conv.conversation_id, r1.task_id)
                assert t1 is not None
                self.assertEqual(t1.status, "completed")
                self.assertTrue(registered_doc_id.startswith("att_"))
                self.assertTrue(registered_img_id.startswith("att_"))

                r2 = await rt.scheduler.dispatch_task(
                    main_conversation_id=conv.conversation_id,
                    role_name="coder",
                    mandate=f"Verify {registered_doc_id}, {registered_img_id}, and art:surcharge_config",
                    work_path=str(ws),
                )
                await rt.scheduler.wait_all()
                t2 = await rt.ticket_store.get_ticket(conv.conversation_id, r2.task_id)
                assert t2 is not None
                self.assertEqual(t2.status, "completed")
                self.assertIn("VERIFIED", coder_verified_report)
                self.assertNotIn("INVALID", coder_verified_report)

                verified_citations = extract_and_verify_citations(
                    t2.result_summary or "",
                    rt.attachment_registry,
                )
                self.assertEqual(len(verified_citations), 3)
                self.assertTrue(all(c.verified for c in verified_citations))


if __name__ == "__main__":
    _ = unittest.main()
