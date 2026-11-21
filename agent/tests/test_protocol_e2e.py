"""Step 12.6: 全链路 ASGI E2E 集成验收与 Schema 契约产物校验。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast, final, override

import httpx
from pydantic_ai.messages import ModelMessage, ModelRequest
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from starlette.applications import Starlette
from starlette.types import Message, Scope

from agent.core import RoleRegistry, RoleSpec, open_runtime
from agent.permissions.reviewer import ApprovalContext, ApprovalItem
from agent.protocol import (
    ApprovalRespondPayload,
    AskUserAnswerItemModel,
    AskUserRespondPayload,
    ItemApprovalDecisionModel,
    build_events_json_schema,
)
from agent.server import build_openapi_document, create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "events.schema.json"
OPENAPI_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "openapi.json"


@final
class _QueueByteStream(httpx.AsyncByteStream):
    def __init__(
        self,
        queue: asyncio.Queue[bytes | None],
        disconnect: asyncio.Event,
        app_task: asyncio.Task[None],
    ) -> None:
        self._queue = queue
        self._disconnect = disconnect
        self._app_task = app_task

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            if chunk:
                yield chunk

    @override
    async def aclose(self) -> None:
        self._disconnect.set()
        if not self._app_task.done():
            _ = self._app_task.cancel()
        try:
            await self._app_task
        except asyncio.CancelledError:
            pass


@final
class _StreamingASGITransport(httpx.AsyncBaseTransport):
    """支持 SSE 长连接增量流式读取的进程内 ASGI Transport（无需真实 socket）。"""

    def __init__(self, app: Starlette) -> None:
        self._app = app

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        raw_headers = [(k.lower(), v) for k, v in request.headers.raw]
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?", 1)[0],
            "query_string": request.url.query,
            "headers": raw_headers,
            "client": ("127.0.0.1", 51234),
            "server": ("127.0.0.1", 8765),
        }
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        started = asyncio.Event()
        disconnect = asyncio.Event()
        req_sent = False
        status_code = 200
        resp_headers: list[tuple[bytes, bytes]] = []

        async def _receive() -> Message:
            nonlocal req_sent
            if not req_sent:
                req_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            _ = await disconnect.wait()
            return {"type": "http.disconnect"}

        async def _send(message: Message) -> None:
            nonlocal status_code, resp_headers
            msg_type = cast(str, message["type"])
            if msg_type == "http.response.start":
                status_code = cast(int, message["status"])
                resp_headers = cast(
                    list[tuple[bytes, bytes]], message.get("headers", [])
                )
                started.set()
            elif msg_type == "http.response.body":
                chunk = cast(bytes, message.get("body", b""))
                if chunk:
                    await queue.put(chunk)
                if not message.get("more_body", False):
                    await queue.put(None)

        async def _run_app() -> None:
            try:
                await self._app(scope, _receive, _send)
            finally:
                if not started.is_set():
                    started.set()
                await queue.put(None)

        app_task = asyncio.create_task(_run_app(), name="asgi-stream-req")
        _ = await started.wait()
        return httpx.Response(
            status_code=status_code,
            headers=resp_headers,
            stream=_QueueByteStream(queue, disconnect, app_task),
        )


class TestStep126E2EAndSchemas(unittest.IsolatedAsyncioTestCase):
    """Step 12.6: 全链路协议 E2E（含 SSE/附件/AskUser/逐条审批/Steer/中断/Fork/9桶）与 Schema 校验。"""

    async def test_schema_artifacts_match_and_valid(self) -> None:
        self.assertTrue(EVENTS_SCHEMA_PATH.is_file(), "events.schema.json must exist")
        self.assertTrue(OPENAPI_SCHEMA_PATH.is_file(), "openapi.json must exist")

        events_doc = cast(
            dict[str, object], json.loads(EVENTS_SCHEMA_PATH.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            events_doc.get("$schema"), "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertEqual(events_doc.get("title"), "AgentProtocolEvent")
        self.assertEqual(events_doc, build_events_json_schema())

        # 检查所有 ProtocolEvent 变体均具备显式 type (const) 与 payload 字段（serde 邻接标签兼容）
        variants = cast(
            list[dict[str, object]],
            events_doc.get("oneOf") or events_doc.get("anyOf") or [],
        )
        self.assertGreaterEqual(len(variants), 20)
        defs = cast(dict[str, dict[str, object]], events_doc.get("$defs", {}))
        seen_types: set[str] = set()
        for var in variants:
            ref = cast(str, var.get("$ref", ""))
            self.assertTrue(ref.startswith("#/$defs/"), f"Unexpected variant ref: {var}")
            def_name = ref.removeprefix("#/$defs/")
            schema_def = defs[def_name]
            props = cast(dict[str, dict[str, object]], schema_def.get("properties", {}))
            self.assertIn("type", props, f"{def_name} missing type")
            self.assertIn("payload", props, f"{def_name} missing payload")
            type_const = props["type"].get("const")
            self.assertIsInstance(type_const, str, f"{def_name} type missing const")
            seen_types.add(cast(str, type_const))
        self.assertIn("runtime.ready", seen_types)
        self.assertIn("message.part.delta", seen_types)
        self.assertIn("interaction.requested", seen_types)
        self.assertIn("context.snapshot", seen_types)

        openapi_doc = cast(
            dict[str, object], json.loads(OPENAPI_SCHEMA_PATH.read_text(encoding="utf-8"))
        )
        self.assertEqual(openapi_doc.get("openapi"), "3.1.0")
        paths = cast(dict[str, object], openapi_doc.get("paths", {}))
        for expected_path in (
            "/v1/health",
            "/v1/runtime/snapshot",
            "/v1/events",
            "/v1/events/subscriptions",
            "/v1/conversations/{cid}/messages",
            "/v1/conversations/{cid}/interrupt",
            "/v1/tree",
            "/v1/conversations/{cid}/fork",
            "/v1/conversations/{cid}/entries",
            "/v1/conversations/{cid}/tasks",
            "/v1/interactions",
            "/v1/interactions/{interaction_id}/respond",
            "/v1/attachments",
            "/v1/attachments/local",
            "/v1/attachments/{att_id}/content",
            "/v1/conversations/{cid}/context",
            "/v1/conversations/{cid}/usage",
            "/v1/skills",
            "/v1/mcps",
        ):
            self.assertIn(expected_path, paths)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            async with open_runtime(
                db_path=p / "c.db",
                step_db_path=p / "s.db",
                media_dir=p / "m",
                enable_protocol_services=True,
            ) as rt:
                app = create_app(rt, auth_token="schema-check")
                self.assertEqual(openapi_doc, build_openapi_document(app))

    async def test_full_protocol_lifecycle_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            seen_interjections: list[str] = []
            steer_gate = asyncio.Event()
            interrupt_gate = asyncio.Event()
            interrupt_started = asyncio.Event()
            role_gate = asyncio.Event()
            run_mode = {"phase": "main"}

            async def router_stream_fn(
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

                if run_mode["phase"] == "interrupt":
                    yield "中断前输出..."
                    interrupt_started.set()
                    _ = await interrupt_gate.wait()
                    yield "不应出现"
                    return

                has_tool_return = any(
                    isinstance(m, ModelRequest)
                    and any(getattr(p, "part_kind", "") == "tool-return" for p in m.parts)
                    for m in messages
                )
                if not has_tool_return:
                    yield {
                        0: DeltaToolCall(
                            name="ask_user_question",
                            json_args=json.dumps(
                                {
                                    "questions": [
                                        {
                                            "header": "策略",
                                            "question": "选择执行策略？",
                                            "options": [
                                                {"label": "快速"},
                                                {"label": "严谨"},
                                            ],
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        )
                    }
                    return

                if not seen_interjections:
                    yield "等待插话..."
                    _ = await steer_gate.wait()
                    return

                yield "主流程已完成并接收插话。"

            async def role_stream_fn(
                _messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str]:
                self.assertIn("ask_user_question", {t.name for t in info.function_tools})
                _ = await role_gate.wait()
                yield "Role 任务完成。"

            roles = RoleRegistry(
                [
                    RoleSpec(
                        name="coder",
                        description="测试编码角色",
                        instructions="你是测试编码角色",
                        model=FunctionModel(stream_function=role_stream_fn),
                        default_lock_mode="shared",
                    )
                ]
            )

            async with open_runtime(
                db_path=tmp_path / "ctx.db",
                step_db_path=tmp_path / "step.db",
                media_dir=tmp_path / "media",
                role_registry=roles,
                enable_protocol_services=True,
            ) as rt:
                # 使用确定性 FunctionModel 替换 Router 真实远端模型
                rt.router._agent.model = FunctionModel(stream_function=router_stream_fn)  # pyright: ignore[reportPrivateUsage]
                conv = await rt.context_tree.ensure_active_conversation(target_title="E2E 主分支")
                cid = conv.conversation_id
                assert rt.event_bus is not None
                assert rt.interaction_broker is not None

                app = create_app(rt, auth_token="e2e-token")
                transport = _StreamingASGITransport(app)
                headers = {"Authorization": "Bearer e2e-token"}

                sse_events: list[dict[str, object]] = []
                sse_ready = asyncio.Event()
                ask_user_event = asyncio.Event()
                approval_event = asyncio.Event()

                async with httpx.AsyncClient(
                    transport=transport, base_url="http://127.0.0.1:8765"
                ) as client:
                    # 1. 首屏快照
                    snap_resp = await client.get("/v1/runtime/snapshot", headers=headers)
                    self.assertEqual(snap_resp.status_code, 200)
                    snap_data = cast(dict[str, object], snap_resp.json())
                    tree_obj = cast(dict[str, object], snap_data["tree"])
                    self.assertEqual(tree_obj["active_conversation_id"], cid)

                    # 2. 启动 SSE 消费者
                    async def _consume_sse() -> None:
                        current_event = ""
                        async with client.stream(
                            "GET",
                            "/v1/events?include_task_streams=true",
                            headers=headers,
                        ) as resp:
                            self.assertEqual(resp.status_code, 200)
                            async for line in resp.aiter_lines():
                                if line.startswith("event:"):
                                    current_event = line.removeprefix("event:").strip()
                                elif line.startswith("data:"):
                                    raw = line.removeprefix("data:").strip()
                                    if not raw:
                                        continue
                                    parsed = cast(dict[str, object], json.loads(raw))
                                    sse_events.append(parsed)
                                    if current_event == "runtime.ready":
                                        sse_ready.set()
                                    elif current_event == "interaction.requested":
                                        payload = cast(dict[str, object], parsed["payload"])
                                        inter = cast(dict[str, object], payload["interaction"])
                                        if inter.get("kind") == "ask_user":
                                            ask_user_event.set()
                                        elif inter.get("kind") == "approval":
                                            approval_event.set()

                    sse_task = asyncio.create_task(_consume_sse(), name="e2e-sse-reader")
                    try:
                        _ = await asyncio.wait_for(sse_ready.wait(), timeout=3.0)

                        # 3. 上传附件 + 发送带附件消息
                        up_resp = await client.post(
                            "/v1/attachments",
                            headers=headers,
                            files={"file": ("spec.txt", b"E2E attachment body", "text/plain")},
                        )
                        self.assertEqual(up_resp.status_code, 201)
                        att_obj = cast(
                            dict[str, object],
                            cast(dict[str, object], up_resp.json())["attachment"],
                        )
                        att_id = cast(str, att_obj["attachment_id"])

                        msg_resp = await client.post(
                            f"/v1/conversations/{cid}/messages",
                            headers=headers,
                            json={
                                "client_message_id": "cli_msg_1",
                                "text": "请结合附件执行任务",
                                "attachment_ids": [att_id],
                            },
                        )
                        self.assertEqual(msg_resp.status_code, 202)

                        # 4. 等待 AskUser 交互并通过 REST 回答（附带 attachment_ids）
                        _ = await asyncio.wait_for(ask_user_event.wait(), timeout=5.0)
                        list_inter = await client.get("/v1/interactions", headers=headers)
                        self.assertEqual(list_inter.status_code, 200)
                        pending = cast(
                            list[dict[str, object]],
                            cast(dict[str, object], list_inter.json())["interactions"],
                        )
                        ask_inter = next(i for i in pending if i["kind"] == "ask_user")
                        ask_iid = cast(str, ask_inter["interaction_id"])

                        ask_reply = AskUserRespondPayload(
                            answers=[
                                AskUserAnswerItemModel(
                                    header="策略",
                                    selected=["严谨"],
                                    attachment_ids=[att_id],
                                )
                            ]
                        )
                        ans_resp = await client.post(
                            f"/v1/interactions/{ask_iid}/respond",
                            headers=headers,
                            content=ask_reply.model_dump_json(),
                        )
                        self.assertEqual(ans_resp.status_code, 200)

                        # 5. 等待主 run 进入 steer_gate 后发送 Steer 插话并放行
                        while not seen_interjections:
                            if rt.router.is_running(cid):
                                steer_resp = await client.post(
                                    f"/v1/conversations/{cid}/messages",
                                    headers=headers,
                                    json={
                                        "client_message_id": "cli_steer_1",
                                        "text": "请顺便检查边界条件",
                                        "delivery_mode": "steer",
                                    },
                                )
                                self.assertEqual(steer_resp.status_code, 202)
                                steer_gate.set()
                                break
                            await asyncio.sleep(0.01)

                        # 等待主 run 完成
                        while rt.router.is_running(cid):
                            await asyncio.sleep(0.01)
                        self.assertTrue(any("请顺便检查边界条件" in s for s in seen_interjections))

                        # 6. 派发 Role Task + 触发同批逐条权限审批（一条放行一条拒绝）
                        dispatch_task = asyncio.create_task(
                            rt.scheduler.dispatch_task(
                                main_conversation_id=cid,
                                role_name="coder",
                                mandate="验证 Role 任务调度与 SSE",
                                work_path=str(tmp_path),
                                lock_mode="shared",
                            )
                        )
                        approval_coro = asyncio.create_task(
                            rt.interaction_broker.request_batch_approval_with_context(
                                [
                                    ApprovalItem(
                                        item_id="item_allow",
                                        tool_name="run_shell",
                                        args={"cmd": "pytest"},
                                        category="shell",
                                        rule_id="shell_exec",
                                        reason="运行 pytest",
                                    ),
                                    ApprovalItem(
                                        item_id="item_deny",
                                        tool_name="edit_file",
                                        args={"path": "config.toml"},
                                        category="edit_file",
                                        rule_id="file_write",
                                        reason="修改配置",
                                    ),
                                ],
                                context=ApprovalContext(
                                    main_conversation_id=cid, run_id="run_batch_1"
                                ),
                            )
                        )
                        _ = await asyncio.wait_for(approval_event.wait(), timeout=3.0)
                        inter_resp = await client.get("/v1/interactions", headers=headers)
                        batch_inter = next(
                            i
                            for i in cast(
                                list[dict[str, object]],
                                cast(dict[str, object], inter_resp.json())["interactions"],
                            )
                            if i["kind"] == "approval"
                        )
                        batch_iid = cast(str, batch_inter["interaction_id"])
                        approval_payload = ApprovalRespondPayload(
                            item_decisions={
                                "item_allow": ItemApprovalDecisionModel(
                                    approved=True, grant_category_for_run=True
                                ),
                                "item_deny": ItemApprovalDecisionModel(
                                    approved=False, reason="先别改配置文件"
                                ),
                            }
                        )
                        decide_resp = await client.post(
                            f"/v1/interactions/{batch_iid}/respond",
                            headers=headers,
                            content=approval_payload.model_dump_json(),
                        )
                        self.assertEqual(decide_resp.status_code, 200)
                        decision = await approval_coro
                        self.assertTrue(decision.item_decisions["item_allow"].approved)
                        self.assertTrue(decision.item_decisions["item_allow"].grant_category_for_run)
                        self.assertFalse(decision.item_decisions["item_deny"].approved)
                        self.assertEqual(
                            decision.item_decisions["item_deny"].reason, "先别改配置文件"
                        )

                        role_gate.set()
                        dispatched = await dispatch_task
                        # 等待 Role 后台完成
                        for _ in range(100):
                            tasks_resp = await client.get(
                                f"/v1/conversations/{cid}/tasks", headers=headers
                            )
                            self.assertEqual(tasks_resp.status_code, 200)
                            tasks_list = cast(
                                list[dict[str, object]],
                                cast(dict[str, object], tasks_resp.json())["tasks"],
                            )
                            if any(
                                t["task_id"] == dispatched.task_id and t["status"] == "done"
                                for t in tasks_list
                            ):
                                break
                            await asyncio.sleep(0.02)

                        # 7. 中断与恢复测试
                        run_mode["phase"] = "interrupt"
                        intr_send = await client.post(
                            f"/v1/conversations/{cid}/messages",
                            headers=headers,
                            json={"client_message_id": "cli_intr_1", "text": "长耗时生成"},
                        )
                        self.assertEqual(intr_send.status_code, 202)
                        _ = await asyncio.wait_for(interrupt_started.wait(), timeout=3.0)
                        intr_resp = await client.post(
                            f"/v1/conversations/{cid}/interrupt", headers=headers
                        )
                        self.assertEqual(intr_resp.status_code, 200)
                        self.assertTrue(cast(dict[str, object], intr_resp.json())["interrupted"])
                        while rt.router.is_running(cid):
                            await asyncio.sleep(0.01)

                        # 8. Tree Fork / Handoff + Entries
                        fork_resp = await client.post(
                            f"/v1/conversations/{cid}/fork",
                            headers=headers,
                            json={"target_title": "E2E Fork 分支", "mode": "handoff"},
                        )
                        self.assertEqual(fork_resp.status_code, 201)
                        tree_resp = await client.get("/v1/tree", headers=headers)
                        self.assertEqual(tree_resp.status_code, 200)
                        branches = cast(
                            list[dict[str, object]],
                            cast(dict[str, object], tree_resp.json())["branches"],
                        )
                        self.assertGreaterEqual(len(branches), 2)

                        entries_resp = await client.get(
                            f"/v1/conversations/{cid}/entries", headers=headers
                        )
                        self.assertEqual(entries_resp.status_code, 200)
                        entries = cast(
                            list[dict[str, object]],
                            cast(dict[str, object], entries_resp.json())["entries"],
                        )
                        self.assertGreaterEqual(len(entries), 2)

                        # 9. Context 9 桶校准与三层用量核对
                        ctx_resp = await client.get(
                            f"/v1/conversations/{cid}/context", headers=headers
                        )
                        self.assertEqual(ctx_resp.status_code, 200)
                        ctx_data = cast(dict[str, object], ctx_resp.json())
                        categories = cast(list[dict[str, object]], ctx_data["categories"])
                        self.assertEqual(len(categories), 9)
                        self.assertEqual(
                            sum(cast(int, c["tokens"]) for c in categories),
                            ctx_data["window_used_tokens"],
                        )

                        usage_resp = await client.get(
                            f"/v1/conversations/{cid}/usage", headers=headers
                        )
                        self.assertEqual(usage_resp.status_code, 200)

                        skills_resp = await client.get("/v1/skills", headers=headers)
                        self.assertEqual(skills_resp.status_code, 200)
                        skills_data = cast(dict[str, object], skills_resp.json())
                        self.assertIn("skills", skills_data)

                        mcps_resp = await client.get("/v1/mcps", headers=headers)
                        self.assertEqual(mcps_resp.status_code, 200)
                        mcps_data = cast(dict[str, object], mcps_resp.json())
                        self.assertIn("servers", mcps_data)

                        # 确认 SSE 全程捕获关键事件类型
                        event_types = {cast(str, e.get("type", "")) for e in sse_events}
                        for required_type in (
                            "runtime.ready",
                            "run.started",
                            "message.part.delta",
                            "interaction.requested",
                            "interaction.resolved",
                            "task.status_changed",
                            "run.completed",
                            "run.interrupted",
                            "tree.branch_created",
                            "context.snapshot",
                        ):
                            self.assertIn(required_type, event_types)
                    finally:
                        steer_gate.set()
                        interrupt_gate.set()
                        role_gate.set()
                        _ = sse_task.cancel()
                        try:
                            await sse_task
                        except asyncio.CancelledError:
                            pass
