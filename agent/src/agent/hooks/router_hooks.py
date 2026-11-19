"""Router 请求级 Hook: 注入待投递后台事件，并在请求末尾动态注入当前激活工作区规则。"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Protocol

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.models import ModelRequestContext

from agent.tools import RouterDeps


class WorkspacePromptPort(Protocol):
    """读取当前激活工作区规则的最小端口协议。"""

    async def get_active_workspace_prompt(self, conversation_id: str) -> str | None: ...


class RouterContextHook(AbstractCapability[RouterDeps]):
    """在每次模型请求前处理事件入队与尾部 AGENTS.md 动态注入（不污染持久化历史）。"""

    def __init__(
        self,
        pending_events: dict[str, deque[str]],
        workspace_prompt: WorkspacePromptPort,
    ) -> None:
        self._pending_events: dict[str, deque[str]] = pending_events
        self._workspace_prompt: WorkspacePromptPort = workspace_prompt

    async def before_model_request(  # pyright: ignore[reportImplicitOverride]
        self, ctx: RunContext[RouterDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        queue = self._pending_events.get(ctx.deps.main_conversation_id)
        if queue:
            while queue:
                _ = ctx.enqueue(queue.popleft(), priority="asap")

        ws_prompt = await self._workspace_prompt.get_active_workspace_prompt(
            ctx.deps.main_conversation_id
        )
        if ws_prompt is not None and request_context.messages:
            ws_msg = ModelRequest(parts=[SystemPromptPart(content=ws_prompt)])
            return replace(
                request_context,
                messages=[*request_context.messages[:-1], ws_msg, request_context.messages[-1]],
            )
        return request_context
