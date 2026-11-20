"""Layer 0 运行时级持久 SSE 事件总线：环形缓冲、Last-Event-ID 重放、20ms Delta 合帧与按需流过滤。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, TypeAdapter

from agent.protocol.events_schema import (
    ArtifactUpdatedEvent,
    AttachmentRegisteredEvent,
    ContextSnapshotEvent,
    EventChannelLiteral,
    InteractionCancelledEvent,
    InteractionRequestedEvent,
    InteractionResolvedEvent,
    LlmPoolStatusEvent,
    MessagePartDeltaEvent,
    MessagePartDeltaPayload,
    MessagePartEndEvent,
    MessagePartStartEvent,
    PartKindLiteral,
    ProtocolEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunInterruptedEvent,
    RunStartedEvent,
    RuntimeReadyEvent,
    RuntimeRecoveryEvent,
    StreamResyncRequiredEvent,
    StreamResyncRequiredPayload,
    TaskCreatedEvent,
    TaskProgressEvent,
    TaskStatusChangedEvent,
    TaskSteeredEvent,
    TreeBranchCreatedEvent,
    TreeBranchSwitchedEvent,
    TreeBranchUpdatedEvent,
    TreeEntryAppendedEvent,
    UsageBudgetWarningEvent,
    UsageStepRecordedEvent,
    WorkspaceLocksChangedEvent,
)

PROTOCOL_EVENT_ADAPTER: TypeAdapter[ProtocolEvent] = TypeAdapter(ProtocolEvent)

_EVENT_CLASSES: tuple[type[BaseModel], ...] = (
    RuntimeReadyEvent,
    RuntimeRecoveryEvent,
    StreamResyncRequiredEvent,
    RunStartedEvent,
    MessagePartStartEvent,
    MessagePartDeltaEvent,
    MessagePartEndEvent,
    RunCompletedEvent,
    RunInterruptedEvent,
    RunFailedEvent,
    InteractionRequestedEvent,
    InteractionResolvedEvent,
    InteractionCancelledEvent,
    TaskCreatedEvent,
    TaskStatusChangedEvent,
    TaskProgressEvent,
    TaskSteeredEvent,
    WorkspaceLocksChangedEvent,
    TreeBranchCreatedEvent,
    TreeBranchSwitchedEvent,
    TreeBranchUpdatedEvent,
    TreeEntryAppendedEvent,
    ContextSnapshotEvent,
    UsageStepRecordedEvent,
    UsageBudgetWarningEvent,
    AttachmentRegisteredEvent,
    ArtifactUpdatedEvent,
    LlmPoolStatusEvent,
)


def _build_event_map() -> dict[str, type[BaseModel]]:
    mapping: dict[str, type[BaseModel]] = {}
    for cls in _EVENT_CLASSES:
        field_info = cls.model_fields["type"]
        default_type = cast(str, field_info.default)
        mapping[default_type] = cls
    return mapping


_EVENT_CLASS_MAP: dict[str, type[BaseModel]] = _build_event_map()


def utc_rfc3339() -> str:
    """生成 UTC RFC3339 时间戳字符串。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


type _DeltaKey = tuple[
    str | None,
    str | None,
    str | None,
    int,
    PartKindLiteral,
    str | None,
    bool,
    EventChannelLiteral,
    str | None,
]


@dataclass
class _PendingDelta:
    chunks: list[str] = field(default_factory=list)
    total_chars: int = 0
    timer: asyncio.TimerHandle | None = None


class EventSubscription:
    """单个 SSE 连接的订阅句柄与过滤状态。"""

    def __init__(
        self,
        *,
        subscription_id: str,
        bus: RuntimeEventBus,
        conversation_id: str | None = None,
        include_task_streams: bool = False,
        task_ids: Collection[str] = (),
        max_queue_size: int = 2048,
    ) -> None:
        self.subscription_id: str = subscription_id
        self._bus: RuntimeEventBus = bus
        self.conversation_id: str | None = conversation_id
        self.include_task_streams: bool = include_task_streams
        self.task_ids: set[str] = set(task_ids)
        self._queue: asyncio.Queue[ProtocolEvent | None] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._closed: bool = False

    def matches(self, event: ProtocolEvent) -> bool:
        """判断事件是否命中当前订阅者的会话与 Task 流过滤条件。"""
        if (
            self.conversation_id is not None
            and event.conversation_id is not None
            and event.conversation_id != self.conversation_id
        ):
            return False
        if event.channel == "task_stream":
            if self.include_task_streams:
                return True
            return event.task_id is not None and event.task_id in self.task_ids
        return True

    def push_nowait(self, event: ProtocolEvent) -> None:
        if self._closed:
            return
        if self._queue.full():
            try:
                _ = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def get(self) -> ProtocolEvent | None:
        return await self._queue.get()

    async def events(self) -> AsyncIterator[ProtocolEvent]:
        try:
            while not self._closed:
                item = await self._queue.get()
                if item is None:
                    break
                yield item
        finally:
            self._bus.unsubscribe(self.subscription_id)


class RuntimeEventBus:
    """运行时级持久 SSE 事件总线（环形缓冲 + Last-Event-ID + 20ms Delta 合帧）。"""

    def __init__(
        self,
        *,
        buffer_size: int = 5000,
        coalesce_window_sec: float = 0.020,
        coalesce_max_chars: int = 16,
    ) -> None:
        self._ring: deque[ProtocolEvent] = deque(maxlen=max(1, buffer_size))
        self._seq: int = 0
        self._coalesce_window_sec: float = max(0.0, coalesce_window_sec)
        self._coalesce_max_chars: int = max(1, coalesce_max_chars)
        self._subscribers: dict[str, EventSubscription] = {}
        self._delta_buffers: dict[_DeltaKey, _PendingDelta] = {}

    @property
    def latest_seq(self) -> int:
        return self._seq

    @property
    def oldest_seq(self) -> int:
        if not self._ring:
            return self._seq + 1
        return self._ring[0].seq

    def publish(
        self,
        event_type: str,
        payload: BaseModel,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        role_name: str | None = None,
        run_id: str | None = None,
        channel: EventChannelLiteral = "main",
    ) -> ProtocolEvent:
        """同步构造、编号、入环并广播事件（若存在同作用域未冲刷 delta 则先保证顺序冲刷）。"""
        if event_type != "message.part.delta":
            self._flush_scope_deltas(
                conversation_id=conversation_id, task_id=task_id, run_id=run_id
            )
        return self._emit_now(
            event_type,
            payload,
            conversation_id=conversation_id,
            task_id=task_id,
            role_name=role_name,
            run_id=run_id,
            channel=channel,
        )

    def emit_delta(
        self,
        delta: str,
        *,
        part_index: int,
        part_kind: PartKindLiteral = "text",
        tool_call_id: str | None = None,
        proactive: bool = False,
        conversation_id: str | None = None,
        task_id: str | None = None,
        role_name: str | None = None,
        run_id: str | None = None,
        channel: EventChannelLiteral = "main",
    ) -> None:
        """合帧发射流式 Delta（20ms 时间窗或累积 >= 16 字符立即冲刷）。"""
        if not delta:
            return
        if self._coalesce_window_sec <= 0.0:
            _ = self._emit_now(
                "message.part.delta",
                MessagePartDeltaPayload(
                    part_index=part_index,
                    part_kind=part_kind,
                    delta=delta,
                    tool_call_id=tool_call_id,
                    proactive=proactive,
                ),
                conversation_id=conversation_id,
                task_id=task_id,
                role_name=role_name,
                run_id=run_id,
                channel=channel,
            )
            return

        key: _DeltaKey = (
            conversation_id,
            task_id,
            run_id,
            part_index,
            part_kind,
            tool_call_id,
            proactive,
            channel,
            role_name,
        )
        pending = self._delta_buffers.get(key)
        if pending is None:
            pending = _PendingDelta()
            self._delta_buffers[key] = pending
        pending.chunks.append(delta)
        pending.total_chars += len(delta)

        if pending.total_chars >= self._coalesce_max_chars:
            self._flush_delta_key(key)
            return

        if pending.timer is None:
            try:
                loop = asyncio.get_running_loop()
                pending.timer = loop.call_later(
                    self._coalesce_window_sec, self._flush_delta_key, key
                )
            except RuntimeError:
                self._flush_delta_key(key)

    def flush_deltas(self) -> None:
        """立即冲刷全部待合帧 Delta。"""
        for key in list(self._delta_buffers.keys()):
            self._flush_delta_key(key)

    def _flush_scope_deltas(
        self,
        *,
        conversation_id: str | None,
        task_id: str | None,
        run_id: str | None,
    ) -> None:
        for key in list(self._delta_buffers.keys()):
            if key[0] == conversation_id and key[1] == task_id and key[2] == run_id:
                self._flush_delta_key(key)

    def _flush_delta_key(self, key: _DeltaKey) -> None:
        pending = self._delta_buffers.pop(key, None)
        if pending is None:
            return
        if pending.timer is not None:
            pending.timer.cancel()
        merged = "".join(pending.chunks)
        if not merged:
            return
        (
            conv_id,
            task_id,
            run_id,
            part_index,
            part_kind,
            tool_call_id,
            proactive,
            channel,
            role_name,
        ) = key
        _ = self._emit_now(
            "message.part.delta",
            MessagePartDeltaPayload(
                part_index=part_index,
                part_kind=part_kind,
                delta=merged,
                tool_call_id=tool_call_id,
                proactive=proactive,
            ),
            conversation_id=conv_id,
            task_id=task_id,
            role_name=role_name,
            run_id=run_id,
            channel=channel,
        )

    def _emit_now(
        self,
        event_type: str,
        payload: BaseModel,
        *,
        conversation_id: str | None,
        task_id: str | None,
        role_name: str | None,
        run_id: str | None,
        channel: EventChannelLiteral,
    ) -> ProtocolEvent:
        cls = _EVENT_CLASS_MAP.get(event_type)
        if cls is None:
            raise ValueError(f"Unknown protocol event type: {event_type!r}")
        self._seq += 1
        event = cast(
            ProtocolEvent,
            cls(
                seq=self._seq,
                event_id=uuid4().hex,
                ts=utc_rfc3339(),
                conversation_id=conversation_id,
                task_id=task_id,
                role_name=role_name,
                run_id=run_id,
                channel=channel,
                payload=payload,
            ),
        )
        self._ring.append(event)
        for sub in list(self._subscribers.values()):
            if sub.matches(event):
                sub.push_nowait(event)
        return event

    def subscribe(
        self,
        *,
        conversation_id: str | None = None,
        include_task_streams: bool = False,
        after_seq: int | None = None,
        task_ids: Collection[str] = (),
    ) -> tuple[EventSubscription, list[ProtocolEvent]]:
        """注册持久订阅并计算断线重放队列或 stream.resync_required。"""
        self.flush_deltas()
        sub_id = uuid4().hex
        sub = EventSubscription(
            subscription_id=sub_id,
            bus=self,
            conversation_id=conversation_id,
            include_task_streams=include_task_streams,
            task_ids=task_ids,
        )
        replay: list[ProtocolEvent] = []
        if after_seq is not None and self._seq > 0:
            oldest = self.oldest_seq
            if after_seq < oldest - 1:
                replay.append(
                    StreamResyncRequiredEvent(
                        seq=self._seq,
                        event_id=uuid4().hex,
                        ts=utc_rfc3339(),
                        payload=StreamResyncRequiredPayload(
                            requested_after_seq=after_seq,
                            oldest_available_seq=oldest,
                            latest_seq=self._seq,
                        ),
                    )
                )
            else:
                for ev in self._ring:
                    if ev.seq > after_seq and sub.matches(ev):
                        replay.append(ev)

        self._subscribers[sub_id] = sub
        return sub, replay

    def update_subscription(
        self, subscription_id: str, *, task_ids: Collection[str]
    ) -> bool:
        """动态更新订阅者关注的后台 Task 细粒度输出流列表。"""
        sub = self._subscribers.get(subscription_id)
        if sub is None:
            return False
        sub.task_ids = set(task_ids)
        return True

    def unsubscribe(self, subscription_id: str) -> None:
        sub = self._subscribers.pop(subscription_id, None)
        if sub is not None:
            sub.close()

    def aclose(self) -> None:
        """取消所有挂起定时合帧并关闭订阅通道。"""
        for pending in self._delta_buffers.values():
            if pending.timer is not None:
                pending.timer.cancel()
        self._delta_buffers.clear()
        for sub_id in list(self._subscribers.keys()):
            self.unsubscribe(sub_id)
