"""Role Worker 请求级 Steer Hook: 运行中实时注入纠偏指令。"""

from __future__ import annotations

from collections import deque

from typing_extensions import override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext


class TaskSteerHook(AbstractCapability[object]):
    """在 Role Worker 每次模型请求前检查并注入运行中收到的 Steer 纠偏指令。"""

    def __init__(self, steer_queue: deque[str]) -> None:
        self._steer_queue: deque[str] = steer_queue

    @override
    async def before_model_request(
        self, ctx: RunContext[object], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        while self._steer_queue:
            instruction = self._steer_queue.popleft()
            _ = ctx.enqueue(
                f"<steer_instruction>\n{instruction}\n</steer_instruction>",
                priority="asap",
            )
        return request_context
