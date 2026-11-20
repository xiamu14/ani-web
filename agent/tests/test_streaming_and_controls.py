"""Step 12.3: request_stream 429 重试、Router 流式/中断/Steer、Role 优雅取消与 AskUser 全量挂载单测。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import override

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

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
from agent.llm import LlmConcurrencyController
from agent.protocol import (
    AskUserAnswerItemModel,
    AskUserRespondPayload,
    InteractionBroker,
    RuntimeEventBus,
)


@dataclass
class _FakeStream(StreamedResponse):
    _chunks: list[str]
    _fail_mid_stream: bool = False

    @override
    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        for idx, ch in enumerate(self._chunks):
            if self._fail_mid_stream and idx == 1:
                raise ModelHTTPError(
                    status_code=429, model_name="fake", body={"error": "mid"}
                )
            for ev in self._parts_manager.handle_text_delta(
                vendor_part_id="text", content=ch
            ):
                yield ev

    @property
    @override
    def usage(self) -> RequestUsage:
        return RequestUsage(input_tokens=10, output_tokens=5)

    @property
    @override
    def model_name(self) -> str:
        return "fake-stream"

    @property
    @override
    def provider_name(self) -> str:
        return "fake-provider"

    @property
    @override
    def provider_url(self) -> str:
        return "https://fake.local"

    @property
    @override
    def timestamp(self) -> datetime:
        return datetime.now(UTC)


class _FlakyStreamModel(Model):
    def __init__(
        self, *, fail_before_open: int = 0, fail_mid_stream: bool = False
    ) -> None:
        super().__init__()
        self.attempts: int = 0
        self._fail_before_open: int = fail_before_open
        self._fail_mid_stream: bool = fail_mid_stream

    @property
    @override
    def model_name(self) -> str:
        return "flaky-stream"

    @property
    @override
    def system(self) -> str:
        return "test"

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        _ = (messages, model_settings, model_request_parameters)
        return ModelResponse(parts=[TextPart(content="ok")])

    @override
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: object = None,
    ) -> AsyncGenerator[StreamedResponse]:
        _ = (messages, model_settings, model_request_parameters, run_context)
        self.attempts += 1
        if self.attempts <= self._fail_before_open:
            raise ModelHTTPError(
                status_code=429, model_name="fake", body={"error": "rate limit"}
            )
        yield _FakeStream(
            _chunks=["hello ", "world"],
            _fail_mid_stream=self._fail_mid_stream,
            model_request_parameters=model_request_parameters,
        )


class TestStep123StreamingAndControls(unittest.IsolatedAsyncioTestCase):
    """Step 12.3: request_stream 429 重试边界、Router 流式/中断/Steer、Role 取消与 AskUser。"""

    async def test_request_stream_retries_pre_first_chunk_429_and_never_mid_stream(
        self,
    ) -> None:
        ctrl = LlmConcurrencyController(
            role_max_concurrency=2,
            rate_limit_max_retries=3,
            base_delay_sec=0.01,
        )
        flaky = _FlakyStreamModel(fail_before_open=2)
        wrapped = ctrl.wrap_role_model(flaky)

        collected: list[str] = []
        async with wrapped.request_stream([], None, ModelRequestParameters()) as stream:
            async for ev in stream:
                if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                    collected.append(ev.part.content)
                elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                    collected.append(ev.delta.content_delta)
        self.assertEqual(flaky.attempts, 3)
        self.assertEqual("".join(collected), "hello world")
        self.assertEqual(ctrl.role_stats.rate_limit_retries, 2)

        # 首包吐出后发生异常绝不重试
        mid_fail = _FlakyStreamModel(fail_before_open=0, fail_mid_stream=True)
        wrapped_mid = ctrl.wrap_role_model(mid_fail)
        with self.assertRaises(ModelHTTPError):
            async with wrapped_mid.request_stream(
                [], None, ModelRequestParameters()
            ) as stream:
                async for _ in stream:
                    pass
        self.assertEqual(mid_fail.attempts, 1)

    async def test_router_streaming_interrupt_steer_and_ask_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_sqlite_async_engine(Path(tmp) / "ctx.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            tree = ContextTreeManager(store)
            _ = await tree.bootstrap()
            conv = await tree.ensure_active_conversation()

            bus = RuntimeEventBus(coalesce_window_sec=0.0)
            broker = InteractionBroker(event_bus=bus)
            sub, _ = bus.subscribe(conversation_id=conv.conversation_id)

            seen_interjections: list[str] = []
            slow_gate = asyncio.Event()

            async def stream_fn(
                messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str | DeltaToolCalls]:
                tool_names = {t.name for t in info.function_tools}
                self.assertIn("ask_user_question", tool_names)

                for msg in messages:
                    if isinstance(msg, ModelRequest):
                        for part in msg.parts:
                            content = getattr(part, "content", "")
                            if isinstance(content, str) and "<user_interjection>" in content:
                                seen_interjections.append(content)

                if len(messages) <= 2:
                    yield {
                        0: DeltaToolCall(
                            name="ask_user_question",
                            json_args=json.dumps(
                                {
                                    "questions": [
                                        {
                                            "header": "模式",
                                            "question": "选哪种模式？",
                                            "options": [
                                                {"label": "快速"},
                                                {"label": "严谨"},
                                            ],
                                        }
                                    ]
                                }
                            ),
                            tool_call_id="call-ask-1",
                        )
                    }
                    return
                yield "已按"
                yield "严谨模式完成"

            router = SuperAgentRouter(
                model=FunctionModel(stream_function=stream_fn),
                context_tree=tree,
                conversation_store=store,
                event_bus=bus,
                interaction_broker=broker,
            )

            run_task = asyncio.create_task(
                router.handle_user_message("帮我选择方案", conversation_id=conv.conversation_id)
            )

            # 消费 SSE 直到遇到 interaction.requested
            ask_iid = ""
            while True:
                ev = await asyncio.wait_for(sub.get(), timeout=2.0)
                assert ev is not None
                if ev.type == "interaction.requested":
                    ask_iid = ev.payload.interaction.interaction_id
                    break

            # 在等待 AskUser 期间实时注入 Steer 建议
            steered = router.steer_conversation(conv.conversation_id, "优先考虑单机性能")
            self.assertTrue(steered)

            _ = await broker.respond(
                ask_iid,
                AskUserRespondPayload(
                    cancelled=False,
                    answers=[AskUserAnswerItemModel(header="模式", selected=["严谨"])],
                ),
            )
            reply = await run_task
            self.assertEqual(reply.output, "已按严谨模式完成")
            self.assertTrue(any("优先考虑单机性能" in s for s in seen_interjections))

            # 测试中断：启动一个慢请求后调用 interrupt_conversation
            async def blocking_stream(
                _messages: list[ModelMessage], _info: AgentInfo
            ) -> AsyncIterator[str]:
                _ = await slow_gate.wait()
                yield "never"

            router_slow = SuperAgentRouter(
                model=FunctionModel(stream_function=blocking_stream),
                context_tree=tree,
                conversation_store=store,
                event_bus=bus,
            )
            slow_task = asyncio.create_task(
                router_slow.handle_user_message("长耗时任务", conversation_id=conv.conversation_id)
            )
            while not router_slow.is_running(conv.conversation_id):
                await asyncio.sleep(0.01)
            interrupted, _, _ = await router_slow.interrupt_conversation(conv.conversation_id)
            self.assertTrue(interrupted)
            slow_reply = await slow_task
            self.assertTrue(slow_reply.interrupted)

            bus.aclose()
            await engine.dispose()

    async def test_scheduler_graceful_cancel_and_role_ask_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_sqlite_async_engine(Path(tmp) / "ctx.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            tickets = SQLModelTicketStore(engine, session_factory=sessions)
            tree = ContextTreeManager(store)
            _ = await tree.bootstrap()
            conv = await tree.ensure_active_conversation()

            bus = RuntimeEventBus(coalesce_window_sec=0.0)
            broker = InteractionBroker(event_bus=bus)
            sub, _ = bus.subscribe(
                conversation_id=conv.conversation_id, include_task_streams=True
            )

            role_gate = asyncio.Event()

            async def role_stream(
                _messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str]:
                tool_names = {t.name for t in info.function_tools}
                self.assertIn("ask_user_question", tool_names)
                _ = await role_gate.wait()
                yield "done"

            roles = RoleRegistry(
                [
                    RoleSpec(
                        name="researcher",
                        description="调研员",
                        instructions="只读调研",
                        model=FunctionModel(stream_function=role_stream),
                        default_lock_mode="shared",
                    )
                ]
            )
            scheduler = WorkspaceLockManager(
                roles=roles,
                sessions=RoleSessionManager(conversation_store=store, ticket_index=tickets),
                ticket_store=tickets,
                event_bus=bus,
                interaction_broker=broker,
            )
            receipt = await scheduler.dispatch_task(
                main_conversation_id=conv.conversation_id,
                role_name="researcher",
                mandate="调研架构",
                work_path=tmp,
            )
            self.assertEqual(receipt.status, "running")
            await asyncio.sleep(0.05)

            msg = await scheduler.cancel_task(
                main_conversation_id=conv.conversation_id, task_id=receipt.task_id
            )
            self.assertIn("已取消", msg)
            await scheduler.wait_all()

            seen_types: list[str] = []
            while True:
                try:
                    ev = await asyncio.wait_for(sub.get(), timeout=0.05)
                except TimeoutError:
                    break
                assert ev is not None
                seen_types.append(ev.type)
            self.assertIn("task.created", seen_types)
            self.assertIn("task.status_changed", seen_types)
            self.assertIn("run.interrupted", seen_types)

            bus.aclose()
            await engine.dispose()
