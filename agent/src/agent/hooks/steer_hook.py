"""Role Worker 请求级 Steer Hook: 运行中实时注入纠偏指令。"""

from __future__ import annotations

from collections import deque
from typing import override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext


class TaskSteerHook(AbstractCapability[object]):
    """在 Role Worker 每次模型请求前检查并注入运行中收到的 Steer 纠偏指令（受 max_injections 上限保护）。"""

    def __init__(self, steer_queue: deque[str], *, max_injections: int = 10) -> None:
        self._steer_queue: deque[str] = steer_queue
        self._max_injections: int = max(0, max_injections)
        self._injected_count: int = 0

    @property
    def injected_count(self) -> int:
        return self._injected_count

    @override
    async def before_model_request(
        self, ctx: RunContext[object], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        while self._steer_queue and self._injected_count < self._max_injections:
            instruction = self._steer_queue.popleft()
            self._injected_count += 1
            _ = ctx.enqueue(
                f"<steer_instruction>\n{instruction}\n</steer_instruction>",
                priority="asap",
            )
        return request_context
