"""Pre-Run 语义路由 Hook: 在进入 Agent.run 前解析或派生目标 Conversation。"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.conversation import Conversation

from agent.context import ContextTreeManager, SemanticBranchRouter


class SemanticRoutingHook:
    """封装显式切换（硬信号）与语义识别建枝/切换（软信号）的前置钩子。"""

    def __init__(
        self,
        context_tree: ContextTreeManager,
        semantic_router: SemanticBranchRouter | None = None,
    ) -> None:
        self._context_tree: ContextTreeManager = context_tree
        self._semantic_router: SemanticBranchRouter | None = semantic_router

    async def resolve_conversation(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        work_path: str | Path | None = None,
    ) -> Conversation:
        """按优先级解析本次请求应使用的目标 Conversation。"""
        if conversation_id is not None:
            return await self._context_tree.switch_conversation(conversation_id)

        if self._semantic_router is not None and self._context_tree.active_conversation_id is not None:
            return await self._route_semantically(prompt, work_path=work_path)

        return await self._context_tree.ensure_active_conversation(work_path=work_path)

    async def _route_semantically(
        self, prompt: str, *, work_path: str | Path | None = None
    ) -> Conversation:
        assert self._semantic_router is not None
        active_id = self._context_tree.active_conversation_id
        assert active_id is not None
        branches = await self._context_tree.list_branches()
        active_branch = next((b for b in branches if b.conversation_id == active_id), None)
        if active_branch is None:
            return await self._context_tree.ensure_active_conversation(work_path=work_path)

        current_conv = await self._context_tree.load_conversation(active_id)
        decision = await self._semantic_router.decide(
            prompt=prompt,
            active_branch=active_branch,
            all_branches=branches,
            recent_messages=current_conv.messages,
        )
        if decision.action == "SWITCH" and decision.target_conversation_id:
            if any(b.conversation_id == decision.target_conversation_id for b in branches):
                return await self._context_tree.switch_conversation(decision.target_conversation_id)
        elif decision.action == "FORK_HANDOFF":
            return await self._context_tree.fork_handoff(
                active_id,
                summary=decision.handoff_summary or decision.new_title or prompt[:80],
                work_path=work_path,
                target_title=decision.new_title,
            )
        elif decision.action == "FORK_ROOT":
            return await self._context_tree.create_root_branch(
                target_title=decision.new_title or prompt[:80],
                work_path=work_path,
            )
        return current_conv
