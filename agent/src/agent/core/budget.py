"""Agent Loop 分层运行预算策略（LoopBudgetPolicy）与安全熔断异常定义（Layer 0 零 IO）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.usage import RunUsage, UsageLimits

LoopAbortKind = Literal["doom_loop", "permission_denied", "steer_overflow"]


class LoopSafetyAbortError(AgentRunError):
    """当 Agent Loop 触发原地死循环（Doom Loop）或重复请求已被拒绝的权限时立即安全熔断。"""

    def __init__(
        self,
        message: str,
        *,
        kind: LoopAbortKind,
        tool_name: str = "",
        streak: int = 0,
    ) -> None:
        super().__init__(message)
        self.kind: LoopAbortKind = kind
        self.tool_name: str = tool_name
        self.streak: int = streak


@dataclass(frozen=True, kw_only=True)
class LoopBudgetPolicy:
    """单次 Agent.run() 的多维安全退出预算与软硬双阈值配置。"""

    max_requests: int = 30
    max_tool_calls: int = 60
    max_total_tokens: int = 200_000
    max_output_tokens: int = 40_000
    wall_clock_timeout_sec: float = 300.0
    soft_limit_ratio: float = 0.80
    max_identical_tool_streak: int = 3
    max_steer_injections: int = 10
    max_retries: int = 3

    def to_usage_limits(self) -> UsageLimits:
        """转换为 Pydantic AI 原生 UsageLimits 硬上限。"""
        return UsageLimits(
            request_limit=self.max_requests,
            tool_calls_limit=self.max_tool_calls,
            total_tokens_limit=self.max_total_tokens,
            output_tokens_limit=self.max_output_tokens,
        )

    @property
    def soft_request_threshold(self) -> int:
        return max(1, int(self.max_requests * self.soft_limit_ratio))

    @property
    def soft_tool_calls_threshold(self) -> int:
        return max(1, int(self.max_tool_calls * self.soft_limit_ratio))

    @property
    def soft_total_tokens_threshold(self) -> int:
        return max(1, int(self.max_total_tokens * self.soft_limit_ratio))

    def is_soft_limit_reached(self, usage: RunUsage, *, next_request_index: int | None = None) -> bool:
        """判断当前累计 Usage（或即将发起的第 N 次请求）是否已达到 80% 软着陆预警线。"""
        req_count = next_request_index if next_request_index is not None else usage.requests
        if req_count >= self.soft_request_threshold:
            return True
        if usage.tool_calls >= self.soft_tool_calls_threshold:
            return True
        total_tok = usage.total_tokens or 0
        return total_tok >= self.soft_total_tokens_threshold


ROUTER_BUDGET_POLICY = LoopBudgetPolicy(
    max_requests=12,
    max_tool_calls=20,
    max_total_tokens=60_000,
    max_output_tokens=16_000,
    wall_clock_timeout_sec=90.0,
    soft_limit_ratio=0.80,
    max_identical_tool_streak=3,
    max_steer_injections=0,
    max_retries=2,
)

READONLY_ROLE_BUDGET_POLICY = LoopBudgetPolicy(
    max_requests=25,
    max_tool_calls=50,
    max_total_tokens=150_000,
    max_output_tokens=32_000,
    wall_clock_timeout_sec=300.0,
    soft_limit_ratio=0.80,
    max_identical_tool_streak=3,
    max_steer_injections=10,
    max_retries=2,
)

READWRITE_ROLE_BUDGET_POLICY = LoopBudgetPolicy(
    max_requests=35,
    max_tool_calls=80,
    max_total_tokens=240_000,
    max_output_tokens=48_000,
    wall_clock_timeout_sec=600.0,
    soft_limit_ratio=0.80,
    max_identical_tool_streak=3,
    max_steer_injections=10,
    max_retries=3,
)


def default_role_budget_policy(role_name: str, *, default_lock_mode: str = "exclusive") -> LoopBudgetPolicy:
    """按角色职责或锁模式推导默认安全退出预算策略。"""
    if role_name in ("researcher", "reviewer") or default_lock_mode == "shared":
        return READONLY_ROLE_BUDGET_POLICY
    return READWRITE_ROLE_BUDGET_POLICY
