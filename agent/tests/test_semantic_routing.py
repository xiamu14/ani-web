"""语义自动建枝、AGENTS.md 末尾切换与显式函数切换测试。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.context import (
    BranchAction,
    BranchDecision,
    BranchInfo,
    ContextTreeManager,
    JevDecisionClient,
    SemanticBranchRouter,
    build_decisions_url,
)
from agent.core import SuperAgentRouter
from agent.db import (
    SQLModelConversationStore,
    create_session_factory,
    create_sqlite_async_engine,
    init_async_db,
)


def _extract_text(messages: list[ModelMessage]) -> str:
    return "\n".join(
        p.content
        for m in messages
        for p in m.parts
        if isinstance(p, (SystemPromptPart, UserPromptPart, TextPart)) and isinstance(p.content, str)
    )


class TestSemanticRoutingAndWorkspaceTail(unittest.IsolatedAsyncioTestCase):
    """验证 AGENTS.md 末尾动态切换、语义自动建枝与显式切换来回跳转。"""

    @asynccontextmanager
    async def _env(
        self,
    ) -> AsyncGenerator[tuple[SQLModelConversationStore, ContextTreeManager, Path]]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = create_sqlite_async_engine(tmp_path / "semantic.db")
            await init_async_db(engine)
            sessions = create_session_factory(engine)
            store = SQLModelConversationStore(engine, session_factory=sessions)
            tree = ContextTreeManager(store)
            _ = await tree.bootstrap()
            try:
                yield store, tree, tmp_path
            finally:
                await engine.dispose()

    async def test_two_turn_workspace_agents_md_tail_switch(self) -> None:
        async with self._env() as (store, tree, tmp_path):
            repo_a = tmp_path / "repo_a"
            repo_b = tmp_path / "repo_b"
            repo_a.mkdir()
            repo_b.mkdir()
            _ = (repo_a / "AGENTS.md").write_text("规则 A: 仅用 pytest", encoding="utf-8")
            _ = (repo_b / "AGENTS.md").write_text("规则 B: 仅用 ruff", encoding="utf-8")

            seen_prompts: list[str] = []
            turn = 0

            def _router_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                nonlocal turn
                seen_prompts.append(_extract_text(messages))
                last = messages[-1]
                if any(p.part_kind == "tool-return" for p in last.parts):
                    return ModelResponse(parts=[TextPart(content=f"turn-{turn}-done")])
                turn += 1
                if turn == 1:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="set_workspace", args={"path": str(repo_a)}, tool_call_id="ws1")]
                    )
                if turn == 2:
                    return ModelResponse(
                        parts=[ToolCallPart(tool_name="set_workspace", args={"path": str(repo_b)}, tool_call_id="ws2")]
                    )
                return ModelResponse(parts=[TextPart(content="turn-3-done")])

            router = SuperAgentRouter(
                model=FunctionModel(_router_model),
                context_tree=tree,
                conversation_store=store,
            )

            # 第 1 轮：绑定 repo_a，工具返回后的下一次模型请求末尾应看到规则 A
            r1 = await router.handle_user_message("先处理 repo_a")
            self.assertEqual(r1.output, "turn-1-done")
            self.assertIn("规则 A: 仅用 pytest", seen_prompts[-1])

            # 第 2 轮：同一 conversation_id 切换工作区到 repo_b（不新建分支）
            r2 = await router.handle_user_message("切到 repo_b")
            self.assertEqual(r2.conversation_id, r1.conversation_id)
            self.assertIn("规则 B: 仅用 ruff", seen_prompts[-1])
            self.assertNotIn("规则 A: 仅用 pytest", seen_prompts[-1])

            # 第 3 轮：继续对话，确认末尾只有规则 B、无规则 A 残留
            r3 = await router.handle_user_message("总结当前目录规则")
            self.assertEqual(r3.conversation_id, r1.conversation_id)
            self.assertIn("规则 B: 仅用 ruff", seen_prompts[-1])
            self.assertNotIn("规则 A: 仅用 pytest", seen_prompts[-1])

            # 持久化历史中不应包含动态注入的规则
            persisted = await store.load(r1.conversation_id, scope="main")
            assert persisted is not None
            persisted_text = _extract_text(persisted.messages)
            self.assertNotIn("规则 A: 仅用 pytest", persisted_text)
            self.assertNotIn("规则 B: 仅用 ruff", persisted_text)

    async def test_semantic_auto_fork_and_explicit_switch_roundtrip(self) -> None:
        async with self._env() as (store, tree, _):
            classifier_actions: list[BranchAction] = ["FORK_ROOT", "FORK_HANDOFF", "CONTINUE"]

            def _classifier_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                action: BranchAction = classifier_actions.pop(0) if classifier_actions else "CONTINUE"
                decision = BranchDecision(
                    action=action,
                    new_title=f"branch-{action}",
                    handoff_summary="交接摘要: CI 已配置完成",
                )
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="final_result",
                            args=decision.model_dump(mode="json"),
                            tool_call_id="cls-1",
                        )
                    ]
                )

            def _chat_model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                return ModelResponse(parts=[TextPart(content=f"msg_count={len(messages)}")])

            semantic_router = SemanticBranchRouter(FunctionModel(_classifier_model))
            router = SuperAgentRouter(
                model=FunctionModel(_chat_model),
                context_tree=tree,
                conversation_store=store,
                semantic_router=semantic_router,
            )

            # 1. 首条消息创建 C1
            c1 = await router.handle_user_message("讨论后端鉴权设计")
            c1_id = c1.conversation_id

            # 2. 语义判定 FORK_ROOT -> 自动新建 C2
            c2 = await router.handle_user_message("完全无关的新任务：配置 GitHub CI")
            c2_id = c2.conversation_id
            self.assertNotEqual(c2_id, c1_id)

            # 3. 语义判定 FORK_HANDOFF -> 基于 C2 派生 C3
            c3 = await router.handle_user_message("基于刚才 CI 结论写一份发布文档")
            c3_id = c3.conversation_id
            self.assertNotIn(c3_id, {c1_id, c2_id})

            # 4. 主动函数调用切换回 C1，再切换回 C2（来回切换且不新建）
            switched_c1 = await router.switch_conversation(c1_id)
            self.assertEqual(switched_c1.conversation_id, c1_id)
            back_c1_reply = await router.handle_user_message("继续鉴权", conversation_id=c1_id)
            self.assertEqual(back_c1_reply.conversation_id, c1_id)

            back_c2_reply = await router.handle_user_message("继续 CI", conversation_id=c2_id)
            self.assertEqual(back_c2_reply.conversation_id, c2_id)

            # 5. 显式切换不存在的分支必须报错（绝不新建）
            with self.assertRaises(LookupError):
                _ = await router.switch_conversation("non-existent-conv")

    async def test_jev_decision_client_url_and_switch_parsing(self) -> None:
        self.assertEqual(build_decisions_url("https://openrouter.ai/api"), "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(build_decisions_url("https://openrouter.ai/api/"), "https://openrouter.ai/api/alpha/decisions")

        seen_urls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "model": "typesafe/jev-1.14",
                    "answers": {
                        "action": {"type": "choice", "choice": "SWITCH"},
                        "target_branch": {"type": "choice", "choice": "conv-auth"},
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            jev = JevDecisionClient(
                api_key="test-key",
                base_url="https://openrouter.ai/api",
                model="typesafe/jev-1.14",
                http_client=client,
            )
            router = SemanticBranchRouter(jev_client=jev)
            b1 = BranchInfo(
                conversation_id="conv-ci", target_title="CI", work_path=None, status="idle", updated_at="2026-11-17"
            )
            b2 = BranchInfo(
                conversation_id="conv-auth", target_title="鉴权", work_path=None, status="idle", updated_at="2026-11-17"
            )
            decision = await router.decide(
                prompt="切回之前的鉴权讨论",
                active_branch=b1,
                all_branches=[b1, b2],
                recent_messages=[],
            )
            self.assertEqual(seen_urls, ["https://openrouter.ai/api/alpha/decisions"])
            self.assertEqual(decision.action, "SWITCH")
            self.assertEqual(decision.target_conversation_id, "conv-auth")


if __name__ == "__main__":
    _ = unittest.main()
