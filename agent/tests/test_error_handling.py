"""统一异常处理架构单元测试（format_exception_detail、RunErrorReportingHook、Worker 与 Router 边界）。"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import final

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.toolsets import FunctionToolset

from agent.context import ContextTreeManager
from agent.core import (
    RoleRegistry,
    RoleSessionManager,
    RoleSpec,
    SuperAgentRouter,
    WorkspaceLockManager,
)
from agent.db import (
    SQLModelConversationStore,
    SQLModelTicketStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)
from agent.hooks import RunErrorReportingHook, format_exception_detail
from agent.protocol import (
    EventSubscription,
    ProtocolEvent,
    RunFailedPayload,
    RuntimeEventBus,
)


class EmptyMessageConnectError(Exception):
    """模拟 httpx.ConnectError('') 等空消息网络异常。"""


async def _drain_subscription(
    sub: EventSubscription,
) -> list[ProtocolEvent]:
    items: list[ProtocolEvent] = []
    while True:
        try:
            ev = await asyncio.wait_for(sub.get(), timeout=0.05)
        except TimeoutError:
            break
        if ev is not None:
            items.append(ev)
    return items


class UnifiedErrorHandlingTest(unittest.IsolatedAsyncioTestCase):
    def test_format_exception_detail_handles_empty_message_and_cause(self) -> None:
        empty_exc = EmptyMessageConnectError("")
        self.assertEqual(
            format_exception_detail(empty_exc), "EmptyMessageConnectError"
        )

        try:
            try:
                raise OSError(61, "Connection refused")
            except OSError as cause:
                raise EmptyMessageConnectError("") from cause
        except EmptyMessageConnectError as chained:
            formatted = format_exception_detail(chained)
            self.assertIn("EmptyMessageConnectError", formatted)
            self.assertIn(
                "cause: ConnectionRefusedError: [Errno 61] Connection refused",
                formatted,
            )

    async def test_worker_tool_empty_connect_error_persists_real_error_and_emits_run_failed(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            engine = create_sqlite_async_engine(Path(tmp) / "ctx.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            tickets = SQLModelTicketStore(engine, session_factory=sessions)
            tree = ContextTreeManager(store)
            _ = await tree.bootstrap()
            conv = await tree.ensure_active_conversation()
            cid = conv.conversation_id

            bus = RuntimeEventBus(coalesce_window_sec=0.0)
            sub, _ = bus.subscribe(
                conversation_id=cid, include_task_streams=True
            )

            def failing_tool() -> str:
                raise EmptyMessageConnectError("")

            def worker_fn(
                messages: list[ModelMessage], info: AgentInfo
            ) -> ModelResponse:
                _ = info
                if len(messages) == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="failing_tool",
                                args={},
                                tool_call_id="call_fail_1",
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="unreachable")])

            async def worker_stream_fn(
                messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str | DeltaToolCalls]:
                _ = info
                if len(messages) == 1:
                    yield {
                        0: DeltaToolCall(
                            name="failing_tool",
                            json_args="{}",
                            tool_call_id="call_fail_1",
                        )
                    }
                else:
                    yield "unreachable"

            roles = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="测试 researcher",
                        instructions="你是 researcher。",
                        model=FunctionModel(
                            worker_fn, stream_function=worker_stream_fn
                        ),
                        toolsets=(FunctionToolset([failing_tool]),),
                        default_lock_mode="shared",
                    )
                ]
            )
            scheduler = WorkspaceLockManager(
                roles=roles,
                sessions=RoleSessionManager(
                    conversation_store=store, ticket_index=tickets
                ),
                ticket_store=tickets,
                event_bus=bus,
            )
            receipt = await scheduler.dispatch_task(
                main_conversation_id=cid,
                role_name="researcher",
                mandate="触发 EmptyMessageConnectError",
                work_path=tmp,
            )
            await scheduler.wait_all()

            row = await tickets.get_ticket(cid, receipt.task_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "failed")
            self.assertIsNotNone(row.result_summary)
            assert row.result_summary is not None
            self.assertIn("EmptyMessageConnectError", row.result_summary)
            self.assertNotEqual(
                row.result_summary, "Worker terminated unexpectedly."
            )

            emitted = await _drain_subscription(sub)
            failed_events = [
                e
                for e in emitted
                if e.type == "run.failed" and e.task_id == receipt.task_id
            ]
            self.assertEqual(len(failed_events), 1)
            payload = failed_events[0].payload
            self.assertIsInstance(payload, RunFailedPayload)
            assert isinstance(payload, RunFailedPayload)
            self.assertIn("EmptyMessageConnectError", payload.error_message)

            bus.aclose()
            await engine.dispose()

    async def test_router_error_emits_single_run_failed_event(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = create_sqlite_async_engine(Path(tmp) / "ctx.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            tree = ContextTreeManager(store)
            _ = await tree.bootstrap()
            conv = await tree.ensure_active_conversation()
            cid = conv.conversation_id

            bus = RuntimeEventBus(coalesce_window_sec=0.0)
            sub, _ = bus.subscribe(conversation_id=cid)

            def failing_router_fn(
                messages: list[ModelMessage], info: AgentInfo
            ) -> ModelResponse:
                _ = (messages, info)
                raise EmptyMessageConnectError("")

            async def failing_router_stream_fn(
                messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str]:
                _ = (messages, info)
                raise EmptyMessageConnectError("")
                yield ""

            router = SuperAgentRouter(
                model=FunctionModel(
                    failing_router_fn, stream_function=failing_router_stream_fn
                ),
                context_tree=tree,
                conversation_store=store,
                event_bus=bus,
            )
            with self.assertRaises(EmptyMessageConnectError) as ctx:
                _ = await router.handle_user_message(
                    "你好",
                    conversation_id=cid,
                    routing_mode="pinned",
                    run_id="run_test_single_fail",
                )
            self.assertIn(
                "_run_failed_emitted",
                getattr(ctx.exception, "__notes__", ()),
            )

            emitted = await _drain_subscription(sub)
            failed_events = [
                e
                for e in emitted
                if e.type == "run.failed"
                and e.run_id == "run_test_single_fail"
            ]
            self.assertEqual(len(failed_events), 1)
            payload = failed_events[0].payload
            self.assertIsInstance(payload, RunFailedPayload)
            assert isinstance(payload, RunFailedPayload)
            self.assertEqual(payload.error_message, "EmptyMessageConnectError")

            bus.aclose()
            await engine.dispose()

    def test_error_reporting_hook_resolves_conversation_id_from_deps(
        self,
    ) -> None:
        hook = RunErrorReportingHook(agent_name="router")

        @final
        class _DummyDeps:
            main_conversation_id: str = "conv_from_deps"

        @final
        class _DummyCtx:
            deps: _DummyDeps = _DummyDeps()

        resolved = hook._resolve_conversation_id(_DummyCtx())  # pyright: ignore[reportArgumentType,reportPrivateUsage]
        self.assertEqual(resolved, "conv_from_deps")


if __name__ == "__main__":
    _ = unittest.main()
