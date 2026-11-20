"""Step 12.2: InteractionBroker、逐条勾选权限审核、AskUser 桥接与 MCP Elicitation 单测。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from typing import cast

from fastmcp.client._sdk_context_shim import RequestContext
from fastmcp.client.elicitation import ElicitRequestParams, ElicitResult
from mcp import ClientSession
from pydantic_ai.messages import TextContent
from pydantic_ai_harness.ask_user import AskUserRequest, Question, QuestionOption

from agent.attachments import AttachmentRegistry
from agent.permissions import (
    ApprovalContext,
    ApprovalDecision,
    ApprovalItem,
    PermissionReviewer,
)
from agent.protocol import (
    ApprovalRespondPayload,
    AskUserAnswerItemModel,
    AskUserRespondPayload,
    InteractionAlreadyResolvedError,
    InteractionBroker,
    InteractionNotFoundError,
    InteractionPayloadError,
    ItemApprovalDecisionModel,
    McpElicitationRespondPayload,
    RuntimeEventBus,
)


class _DummyParams:
    def __init__(self, requested_schema: dict[str, object]) -> None:
        self.requestedSchema: dict[str, object] = requested_schema


class TestStep122InteractionBroker(unittest.IsolatedAsyncioTestCase):
    """Step 12.2: 逐条勾选审批、AskUser 附件句柄转换、MCP Elicitation 与 Scope 取消。"""

    async def test_per_item_granular_approval_fail_closed_and_run_grant(self) -> None:
        bus = RuntimeEventBus(coalesce_window_sec=0.0)
        broker = InteractionBroker(event_bus=bus)
        sub, _ = bus.subscribe(conversation_id="conv-1")

        reviewer = PermissionReviewer(
            approval_port=broker,
            batch_window_seconds=0.015,
        )
        ctx = ApprovalContext(
            main_conversation_id="conv-1",
            task_id="T1",
            role_name="coder",
            run_id="run-1",
            user_intent="运行命令并修改配置",
        )

        # 同轮并发触发 3 个需要 REVIEW 的操作
        t1 = asyncio.create_task(
            reviewer.authorize(
                run_id="run-1",
                tool_name="shell",
                args={"command": "uv add httpx"},
                workspace_root=Path("/tmp/ws"),
                user_intent="install httpx",
                context=ctx,
            )
        )
        t2 = asyncio.create_task(
            reviewer.authorize(
                run_id="run-1",
                tool_name="edit_file",
                args={"path": "/tmp/ws/pyproject.toml", "replacement": "x"},
                workspace_root=Path("/tmp/ws"),
                user_intent="edit pyproject",
                context=ctx,
            )
        )
        t3 = asyncio.create_task(
            reviewer.authorize(
                run_id="run-1",
                tool_name="mcp_unregistered_tool",
                args={"q": "1"},
                workspace_root=Path("/tmp/ws"),
                user_intent="call mcp",
                is_mcp_tool=True,
                context=ctx,
            )
        )

        req_event = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert req_event is not None
        self.assertEqual(req_event.type, "interaction.requested")
        assert req_event.type == "interaction.requested"
        interaction = req_event.payload.interaction
        self.assertEqual(interaction.kind, "approval")
        assert interaction.approval is not None
        self.assertEqual(len(interaction.approval.items), 3)
        self.assertEqual(interaction.approval.context.task_id, "T1")

        by_tool = {item.tool_name: item for item in interaction.approval.items}
        item_shell = by_tool["shell"]
        item_edit = by_tool["edit_file"]
        # 故意漏掉 item_mcp，验证 fail-closed 默认拒绝
        resolved_view = await broker.respond(
            interaction.interaction_id,
            ApprovalRespondPayload(
                item_decisions={
                    item_shell.item_id: ItemApprovalDecisionModel(
                        approved=True,
                        grant_category_for_run=True,
                        reason="允许安装包",
                    ),
                    item_edit.item_id: ItemApprovalDecisionModel(
                        approved=False,
                        grant_category_for_run=False,
                        reason="先别改配置文件",
                    ),
                }
            ),
        )
        self.assertEqual(resolved_view.status, "resolved")

        res1, res2, res3 = await asyncio.gather(t1, t2, t3)
        self.assertTrue(res1.allowed)
        self.assertEqual(res1.decision_source, "user:approved")
        self.assertFalse(res2.allowed)
        self.assertIn("先别改配置文件", res2.reason)
        self.assertFalse(res3.allowed)
        self.assertIn("默认拒绝", res3.reason)

        # 验证 item_shell 的 grant_category_for_run 在同 run_id 下生效（不再弹窗）
        res1_again = await reviewer.authorize(
            run_id="run-1",
            tool_name="shell",
            args={"command": "uv add ruff"},
            workspace_root=Path("/tmp/ws"),
            user_intent="install ruff",
            context=ctx,
        )
        self.assertTrue(res1_again.allowed)
        self.assertEqual(res1_again.decision_source, "run_grant")

        # 重复提交应抛出 InteractionAlreadyResolvedError
        with self.assertRaises(InteractionAlreadyResolvedError):
            _ = await broker.respond(
                interaction.interaction_id,
                ApprovalRespondPayload(approved=True),
            )
        with self.assertRaises(InteractionNotFoundError):
            _ = await broker.respond(
                "non-existent-id",
                ApprovalRespondPayload(approved=True),
            )

        bus.aclose()

    async def test_backward_compatible_batch_approval(self) -> None:
        class _LegacyPort:
            async def request_batch_approval(
                self,
                items: object,
                *,
                user_intent: str = "",
            ) -> ApprovalDecision:
                _ = (items, user_intent)
                return ApprovalDecision(
                    approved=True,
                    grant_categories_for_run=("external_network_or_pkg",),
                    reason="legacy ok",
                )

        reviewer = PermissionReviewer(approval_port=_LegacyPort())
        res = await reviewer.authorize(
            run_id="run-legacy",
            tool_name="shell",
            args={"command": "curl https://example.com"},
            workspace_root=Path("/tmp/ws"),
        )
        self.assertTrue(res.allowed)
        self.assertEqual(res.reason, "legacy ok")

    async def test_ask_user_bridge_with_attachment_handle_in_note(self) -> None:
        bus = RuntimeEventBus(coalesce_window_sec=0.0)
        registry = AttachmentRegistry()
        att = registry.register_bytes(
            b"fake-png-bytes", title="arch.png", media_type="image/png"
        )
        broker = InteractionBroker(event_bus=bus, attachment_registry=registry)
        sub, _ = bus.subscribe(conversation_id="conv-1")

        req = AskUserRequest(
            questions=(
                Question(
                    header="存储方案",
                    question="请选择持久化方案",
                    options=(
                        QuestionOption(label="SQLite WAL", description="轻量单机"),
                        QuestionOption(label="Postgres", description="多机并发"),
                    ),
                    multi_select=False,
                ),
            )
        )
        task = asyncio.create_task(
            broker.answer_ask_user(
                req,
                conversation_id="conv-1",
                task_id=None,
                role_name="router",
                run_id="run-router",
            )
        )

        ev = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert ev is not None and ev.type == "interaction.requested"
        iid = ev.payload.interaction.interaction_id

        # 错配 kind 应抛出 InteractionPayloadError
        with self.assertRaises(InteractionPayloadError):
            _ = await broker.respond(iid, ApprovalRespondPayload(approved=True))

        _ = await broker.respond(
            iid,
            AskUserRespondPayload(
                cancelled=False,
                answers=[
                    AskUserAnswerItemModel(
                        header="存储方案",
                        selected=["SQLite WAL"],
                        note_text="参考架构图",
                        attachment_ids=[att.attachment_id],
                    )
                ],
            ),
        )
        resp = await task
        self.assertFalse(resp.cancelled)
        self.assertEqual(len(resp.answers), 1)
        self.assertEqual(resp.answers[0].selected, ("SQLite WAL",))
        note_item = resp.answers[0].note[0]
        self.assertIsInstance(note_item, TextContent)
        assert isinstance(note_item, TextContent)
        note_text = note_item.content
        self.assertIn("参考架构图", note_text)
        self.assertIn(f"[[attachment:{att.attachment_id}", note_text)

        bus.aclose()

    async def test_mcp_elicitation_and_cancel_for_scope(self) -> None:
        bus = RuntimeEventBus(coalesce_window_sec=0.0)
        broker = InteractionBroker(event_bus=bus)
        sub, _ = bus.subscribe(conversation_id="conv-1")

        handler = broker.make_mcp_elicitation_handler(
            conversation_id="conv-1",
            task_id="T2",
            role_name="researcher",
            server_name="github",
        )
        async def _run_elicit() -> ElicitResult[dict[str, object]]:
            raw_res = await handler(
                "请输入仓库名",
                None,
                cast(
                    ElicitRequestParams,
                    cast(
                        object,
                        _DummyParams(
                            {"type": "object", "properties": {"repo": {"type": "string"}}}
                        ),
                    ),
                ),
                cast(RequestContext[ClientSession, object], object()),
            )
            return cast(ElicitResult[dict[str, object]], raw_res)

        elicit_task = asyncio.create_task(_run_elicit())

        ev = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert ev is not None and ev.type == "interaction.requested"
        iid = ev.payload.interaction.interaction_id
        _ = await broker.respond(
            iid,
            McpElicitationRespondPayload(
                action="accept", content={"repo": "ani-web"}
            ),
        )
        elicit_res = await elicit_task
        self.assertEqual(elicit_res.action, "accept")
        self.assertEqual(elicit_res.content, {"repo": "ani-web"})

        # 验证 cancel_for_scope 能安全唤醒挂起中的审批并广播 interaction.cancelled
        pending_task = asyncio.create_task(
            broker.request_batch_approval_with_context(
                [
                    ApprovalItem(
                        tool_name="shell",
                        args={"command": "rm a.txt"},
                        category="shell_mutation",
                        rule_id="review.cmd",
                        reason="delete file",
                    )
                ],
                context=ApprovalContext(
                    main_conversation_id="conv-1",
                    task_id="T9",
                    role_name="coder",
                    run_id="run-t9",
                ),
            )
        )
        req2 = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert req2 is not None
        # Drain interaction.resolved from previous elicit
        if req2.type == "interaction.resolved":
            req2 = await asyncio.wait_for(sub.get(), timeout=1.0)
            assert req2 is not None
        self.assertEqual(req2.type, "interaction.requested")

        cancelled_cnt = broker.cancel_for_scope(task_id="T9", reason="run_cancelled")
        self.assertEqual(cancelled_cnt, 1)
        dec = await pending_task
        self.assertFalse(dec.approved)

        cancel_ev = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert cancel_ev is not None
        self.assertEqual(cancel_ev.type, "interaction.cancelled")
        bus.aclose()
