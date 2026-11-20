"""Step 12.1: Layer 0 协议契约模型、Schema 导出与 RuntimeEventBus 持久 SSE 总线单测。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from agent.protocol import (
    PROTOCOL_EVENT_ADAPTER,
    PROTOCOL_VERSION,
    ApprovalContextView,
    ApprovalItemView,
    ApprovalPayloadView,
    ApprovalRespondPayload,
    AuxJevUsageView,
    ContextBucketView,
    ContextSubItemView,
    ContextWindowSnapshotView,
    ErrorEnvelope,
    InteractionRequestedPayload,
    InteractionRespondRequest,
    InteractionView,
    ItemApprovalDecisionModel,
    MessagePartEndPayload,
    MessagePartStartPayload,
    ProtocolEvent,
    RoleTaskUsageItemView,
    RunCompletedPayload,
    RuntimeEventBus,
    RuntimeReadyPayload,
    RunUsageView,
    TaskStatusChangedPayload,
    TaskView,
    TreeUsageSummaryView,
    build_events_json_schema,
    utc_rfc3339,
    write_events_schema_file,
)


class TestStep121ProtocolModelsAndSchema(unittest.TestCase):
    """Step 12.1: 协议模型严格校验、Rust serde 判别标签与 JSON Schema 导出测试。"""

    def test_strict_models_reject_extra_fields_and_round_trip(self) -> None:
        err = ErrorEnvelope(
            code="NOT_FOUND",
            message="Conversation not found",
            detail={"conversation_id": "conv-1"},
            retryable=False,
            request_id="req-1",
        )
        restored_err = ErrorEnvelope.model_validate_json(err.model_dump_json())
        self.assertEqual(restored_err, err)
        self.assertEqual(restored_err.protocol_version, PROTOCOL_VERSION)

        with self.assertRaises(ValidationError):
            _ = ErrorEnvelope.model_validate(
                {
                    "code": "X",
                    "message": "Y",
                    "request_id": "r1",
                    "unexpected_field": 123,
                }
            )

        snap = ContextWindowSnapshotView(
            conversation_id="conv-1",
            run_id="run-1",
            model_name="gpt-4o",
            window_used_tokens=10000,
            window_limit_tokens=128000,
            limit_known=True,
            utilization_ratio=10000 / 128000,
            cache_hit_ratio=0.6,
            uncached_input_tokens=4000,
            calibrated=True,
            categories=[
                ContextBucketView(
                    category="system_prompt",
                    tokens=1200,
                    ratio=0.12,
                    sub_items=[ContextSubItemView(name="global_agents_md", tokens=1200)],
                )
            ],
            ts=utc_rfc3339(),
        )
        self.assertEqual(
            ContextWindowSnapshotView.model_validate_json(snap.model_dump_json()),
            snap,
        )

        tree_usage = TreeUsageSummaryView(
            conversation_id="conv-1",
            main_router_usage=RunUsageView(input_tokens=1000, output_tokens=200, total_tokens=1200, requests=1),
            role_tasks_usage=[
                RoleTaskUsageItemView(
                    task_id="T1",
                    role_name="coder",
                    usage=RunUsageView(input_tokens=5000, cache_read_tokens=3000, output_tokens=800, total_tokens=5800),
                )
            ],
            role_tasks_total=RunUsageView(input_tokens=5000, cache_read_tokens=3000, output_tokens=800, total_tokens=5800),
            auxiliary_jev_usage=[
                AuxJevUsageView(source="semantic_router", requests=1, input_tokens=150, output_tokens=20, total_tokens=170)
            ],
        )
        self.assertEqual(
            TreeUsageSummaryView.model_validate_json(tree_usage.model_dump_json()),
            tree_usage,
        )

    def test_discriminated_interaction_respond_and_protocol_event_round_trip(self) -> None:
        respond_adapter: TypeAdapter[InteractionRespondRequest] = TypeAdapter(
            InteractionRespondRequest
        )
        raw_approval = json.dumps(
            {
                "kind": "approval",
                "item_decisions": {
                    "item_1": {
                        "approved": True,
                        "grant_category_for_run": True,
                        "reason": "ok",
                    }
                },
            }
        )
        parsed = respond_adapter.validate_json(raw_approval)
        self.assertIsInstance(parsed, ApprovalRespondPayload)
        assert isinstance(parsed, ApprovalRespondPayload)
        self.assertEqual(
            parsed.item_decisions["item_1"],
            ItemApprovalDecisionModel(
                approved=True, grant_category_for_run=True, reason="ok"
            ),
        )

        interaction = InteractionView(
            interaction_id="int-1",
            kind="approval",
            conversation_id="conv-1",
            task_id="T1",
            role_name="coder",
            run_id="run-1",
            created_at=utc_rfc3339(),
            approval=ApprovalPayloadView(
                items=[
                    ApprovalItemView(
                        item_id="item_1",
                        tool_name="shell",
                        args={"command": "uv add httpx"},
                        category="external_network_or_pkg",
                        rule_id="review.cmd.package_or_network",
                        reason="Package install",
                    )
                ],
                context=ApprovalContextView(
                    main_conversation_id="conv-1",
                    task_id="T1",
                    role_name="coder",
                    run_id="run-1",
                    user_intent="install httpx",
                ),
            ),
        )
        bus = RuntimeEventBus()
        ev = bus.publish(
            "interaction.requested",
            InteractionRequestedPayload(interaction=interaction),
            conversation_id="conv-1",
            task_id="T1",
            role_name="coder",
            run_id="run-1",
        )
        restored = PROTOCOL_EVENT_ADAPTER.validate_json(ev.model_dump_json())
        self.assertEqual(restored.type, "interaction.requested")
        self.assertEqual(restored.seq, 1)
        bus.aclose()

    def test_events_json_schema_export_contains_discriminator(self) -> None:
        schema = build_events_json_schema()
        self.assertEqual(schema.get("title"), "AgentProtocolEvent")
        self.assertEqual(
            schema.get("$schema"), "https://json-schema.org/draft/2020-12/schema"
        )
        discriminator = cast(dict[str, object], schema.get("discriminator", {}))
        self.assertEqual(discriminator.get("propertyName"), "type")
        mapping = cast(dict[str, object], discriminator.get("mapping", {}))
        self.assertIn("runtime.ready", mapping)
        self.assertIn("message.part.delta", mapping)
        self.assertIn("interaction.requested", mapping)
        self.assertIn("context.snapshot", mapping)

        with tempfile.TemporaryDirectory() as tmp:
            out = write_events_schema_file(Path(tmp) / "events.schema.json")
            self.assertTrue(out.is_file())
            loaded = cast(dict[str, object], json.loads(out.read_text(encoding="utf-8")))
            self.assertEqual(loaded["title"], "AgentProtocolEvent")


class TestStep121RuntimeEventBus(unittest.IsolatedAsyncioTestCase):
    """Step 12.1: RingBuffer 重放、resync_required、20ms Delta 合帧与 Task 流动态过滤。"""

    async def test_ring_buffer_replay_and_resync_required(self) -> None:
        bus = RuntimeEventBus(buffer_size=5, coalesce_window_sec=0.0)
        for i in range(1, 9):
            _ = bus.publish(
                "runtime.ready",
                RuntimeReadyPayload(
                    latest_seq=i,
                    active_conversation_id="conv-1",
                    model_name="gpt-4o",
                    trusted_local=True,
                    subscription_id=f"sub-{i}",
                ),
                conversation_id="conv-1",
            )
        self.assertEqual(bus.latest_seq, 8)
        self.assertEqual(bus.oldest_seq, 4)

        sub_ok, replay_ok = bus.subscribe(after_seq=5)
        self.assertEqual([e.seq for e in replay_ok], [6, 7, 8])
        bus.unsubscribe(sub_ok.subscription_id)

        sub_stale, replay_stale = bus.subscribe(after_seq=1)
        self.assertEqual(len(replay_stale), 1)
        self.assertEqual(replay_stale[0].type, "stream.resync_required")
        if replay_stale[0].type == "stream.resync_required":
            self.assertEqual(replay_stale[0].payload.requested_after_seq, 1)
            self.assertEqual(replay_stale[0].payload.oldest_available_seq, 4)
            self.assertEqual(replay_stale[0].payload.latest_seq, 8)
        bus.unsubscribe(sub_stale.subscription_id)
        bus.aclose()

    async def test_delta_coalescing_timer_and_ordered_flush_on_part_end(self) -> None:
        bus = RuntimeEventBus(coalesce_window_sec=0.020, coalesce_max_chars=16)
        sub, _ = bus.subscribe(conversation_id="conv-1")

        # 1. 发送 10 个 1 字符片段 (< 16 字符)，应合帧为 1 条
        for ch in "abcdefghij":
            bus.emit_delta(
                ch,
                part_index=0,
                part_kind="text",
                conversation_id="conv-1",
                run_id="run-1",
            )
        await asyncio.sleep(0.040)

        ev1 = await sub.get()
        assert ev1 is not None
        self.assertEqual(ev1.type, "message.part.delta")
        if ev1.type == "message.part.delta":
            self.assertEqual(ev1.payload.delta, "abcdefghij")

        # 2. 发送短片段后立即调用 message.part.end，必须先冲刷 delta 再发 part.end
        bus.emit_delta(
            "xyz",
            part_index=0,
            part_kind="text",
            conversation_id="conv-1",
            run_id="run-1",
        )
        _ = bus.publish(
            "message.part.end",
            MessagePartEndPayload(part_index=0, part_kind="text"),
            conversation_id="conv-1",
            run_id="run-1",
        )
        ev2 = await sub.get()
        ev3 = await sub.get()
        assert ev2 is not None and ev3 is not None
        self.assertEqual(ev2.type, "message.part.delta")
        self.assertEqual(ev3.type, "message.part.end")
        if ev2.type == "message.part.delta":
            self.assertEqual(ev2.payload.delta, "xyz")

        # 3. 单次 >= 16 字符应立即冲刷无需等待 20ms
        bus.emit_delta(
            "0123456789abcdef",
            part_index=1,
            part_kind="thinking",
            conversation_id="conv-1",
            run_id="run-1",
        )
        ev4 = await asyncio.wait_for(sub.get(), timeout=0.01)
        assert ev4 is not None
        self.assertEqual(ev4.type, "message.part.delta")

        bus.aclose()

    async def test_task_stream_filtering_and_dynamic_subscription(self) -> None:
        bus = RuntimeEventBus(coalesce_window_sec=0.0)
        sub, _ = bus.subscribe(conversation_id="conv-1", include_task_streams=False)

        now = utc_rfc3339()
        task_view = TaskView(
            main_conversation_id="conv-1",
            task_id="T2",
            role_name="coder",
            mandate="fix parser",
            status="running",
            live_phase="thinking",
            created_at=now,
            updated_at=now,
        )
        # 主通道 task.status_changed 应收到
        _ = bus.publish(
            "task.status_changed",
            TaskStatusChangedPayload(task=task_view),
            conversation_id="conv-1",
            task_id="T2",
            role_name="coder",
            channel="main",
        )
        # task_stream 通道在未订阅 T2 时应被过滤
        _ = bus.publish(
            "message.part.start",
            MessagePartStartPayload(part_index=0, part_kind="text"),
            conversation_id="conv-1",
            task_id="T2",
            role_name="coder",
            run_id="run-t2",
            channel="task_stream",
        )
        bus.emit_delta(
            "hidden-delta",
            part_index=0,
            conversation_id="conv-1",
            task_id="T2",
            role_name="coder",
            run_id="run-t2",
            channel="task_stream",
        )

        # 动态开启 T2 细粒度流订阅
        updated = bus.update_subscription(sub.subscription_id, task_ids=["T2"])
        self.assertTrue(updated)
        bus.emit_delta(
            "visible-delta",
            part_index=0,
            conversation_id="conv-1",
            task_id="T2",
            role_name="coder",
            run_id="run-t2",
            channel="task_stream",
        )
        _ = bus.publish(
            "run.completed",
            RunCompletedPayload(output_text="done"),
            conversation_id="conv-1",
            task_id="T2",
            role_name="coder",
            run_id="run-t2",
            channel="task_stream",
        )

        received: list[ProtocolEvent] = []
        for _ in range(3):
            item = await asyncio.wait_for(sub.get(), timeout=0.1)
            assert item is not None
            received.append(item)

        self.assertEqual(
            [e.type for e in received],
            ["task.status_changed", "message.part.delta", "run.completed"],
        )
        if received[1].type == "message.part.delta":
            self.assertEqual(received[1].payload.delta, "visible-delta")

        bus.aclose()
