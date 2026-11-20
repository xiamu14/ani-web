"""LLM 请求并发控制器（双池隔离：Router 快车道 + Role 共享池）与全局 429 协同退避冷却门。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, override

from pydantic_ai import RunContext
from pydantic_ai.concurrency import ConcurrencyLimit, ConcurrencyLimiter
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import (
    KnownModelName,
    Model,
    ModelRequestContext,
    ModelRequestParameters,
    StreamedResponse,
    infer_model,
)
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage


def is_rate_limit_error(exc: Exception) -> bool:
    """判断模型或 HTTP 异常是否为上游 429 Rate Limit / 并发超限错误。"""
    if isinstance(exc, ModelHTTPError) and exc.status_code == 429:
        return True
    if isinstance(exc, ModelAPIError):
        msg = str(exc).lower()
        if "429" in msg or "rate limit" in msg or "too many requests" in msg or "concurrency" in msg:
            return True
    return False


def parse_retry_after_header(value: str | None) -> float | None:
    """解析 HTTP Retry-After 秒数头（忽略不合法或负值）。"""
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value.strip())
        return parsed if parsed > 0.0 else None
    except ValueError:
        return None


@dataclass
class ConcurrencyPoolStats:
    """单个限流池的实时在途请求数、历史并发峰值与累计请求计数。"""

    current_in_flight: int = 0
    peak_in_flight: int = 0
    total_requests: int = 0
    rate_limit_retries: int = 0

    def on_enter(self) -> None:
        self.current_in_flight += 1
        self.total_requests += 1
        self.peak_in_flight = max(self.peak_in_flight, self.current_in_flight)

    def on_exit(self) -> None:
        self.current_in_flight = max(0, self.current_in_flight - 1)


class RateLimitCooldownGate:
    """全局 429 协同退避冷却门：任一协程遇到 429 时统一推迟后续并发请求，消除惊群重试风暴。"""

    def __init__(
        self,
        *,
        base_delay_sec: float = 0.5,
        max_delay_sec: float = 15.0,
    ) -> None:
        self._base_delay_sec: float = max(0.01, base_delay_sec)
        self._max_delay_sec: float = max(self._base_delay_sec, max_delay_sec)
        self._cooldown_until: float = 0.0
        self._total_429_hits: int = 0
        self._total_cooldown_waits: int = 0

    @property
    def total_429_hits(self) -> int:
        return self._total_429_hits

    @property
    def total_cooldown_waits(self) -> int:
        return self._total_cooldown_waits

    async def wait_if_cooling_down(self) -> None:
        """若当前处于全局 429 冷却期，则暂停等待直至冷却窗口结束。"""
        waited = False
        while True:
            remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0.0:
                break
            waited = True
            await asyncio.sleep(remaining)
        if waited:
            self._total_cooldown_waits += 1

    def notify_rate_limited(
        self,
        attempt: int = 0,
        *,
        retry_after_sec: float | None = None,
    ) -> float:
        """记录一次 429 事件并返回推进后的冷却延迟秒数。"""
        self._total_429_hits += 1
        if retry_after_sec is not None and retry_after_sec > 0.0:
            delay = min(retry_after_sec, self._max_delay_sec)
        else:
            shift = min(max(0, attempt), 10)
            exp = self._base_delay_sec * float(1 << shift)
            jitter = random.uniform(0.05, 0.25) * self._base_delay_sec
            delay = min(exp + jitter, self._max_delay_sec)
        target = time.monotonic() + delay
        self._cooldown_until = max(self._cooldown_until, target)
        return delay


class CooldownRetryModel(WrapperModel):
    """在持有 ConcurrencyLimiter 槽位期间执行全局 429 冷却等待、退避重试与并发水位统计。"""

    def __init__(
        self,
        wrapped: Model | KnownModelName | str,
        *,
        cooldown_gate: RateLimitCooldownGate,
        stats: ConcurrencyPoolStats,
        max_retries: int = 3,
        pool_tag: str = "role",
    ) -> None:
        super().__init__(wrapped)
        self._gate: RateLimitCooldownGate = cooldown_gate
        self._stats: ConcurrencyPoolStats = stats
        self._max_retries: int = max(0, max_retries)
        self._pool_tag: str = pool_tag

    @property
    def pool_tag(self) -> str:
        return self._pool_tag

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        attempt = 0
        while True:
            await self._gate.wait_if_cooling_down()
            self._stats.on_enter()
            try:
                return await self.wrapped.request(
                    messages, model_settings, model_request_parameters
                )
            except (ModelHTTPError, ModelAPIError) as exc:
                if not is_rate_limit_error(exc) or attempt >= self._max_retries:
                    raise
                self._stats.rate_limit_retries += 1
                _ = self._gate.notify_rate_limited(attempt)
                attempt += 1
            finally:
                self._stats.on_exit()

    @override
    async def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> RequestUsage:
        await self._gate.wait_if_cooling_down()
        self._stats.on_enter()
        try:
            return await self.wrapped.count_tokens(
                messages, model_settings, model_request_parameters
            )
        finally:
            self._stats.on_exit()

    @override
    async def compact_messages(
        self,
        request_context: ModelRequestContext,
        *,
        instructions: str | None = None,
    ) -> ModelResponse:
        await self._gate.wait_if_cooling_down()
        self._stats.on_enter()
        try:
            return await self.wrapped.compact_messages(
                request_context, instructions=instructions
            )
        finally:
            self._stats.on_exit()

    @override
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,  # pyright: ignore[reportExplicitAny]
    ) -> AsyncGenerator[StreamedResponse]:
        attempt = 0
        while True:
            await self._gate.wait_if_cooling_down()
            self._stats.on_enter()
            entered_stream = False
            try:
                async with self.wrapped.request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as stream:
                    entered_stream = True
                    yield stream
                    return
            except (ModelHTTPError, ModelAPIError) as exc:
                # 仅在进入流并向调用方吐出首包前对 429 执行退避重试；首包之后禁止重试
                if (
                    entered_stream
                    or not is_rate_limit_error(exc)
                    or attempt >= self._max_retries
                ):
                    raise
                self._stats.rate_limit_retries += 1
                _ = self._gate.notify_rate_limited(attempt)
                attempt += 1
            finally:
                self._stats.on_exit()


def _unwrap_raw_model(model: Model | KnownModelName | str) -> Model:
    """剥离已有的 ConcurrencyLimitedModel / CooldownRetryModel 外壳以支持幂等重新绑定指定池。"""
    resolved = infer_model(model) if isinstance(model, str) else model
    curr = resolved
    while isinstance(curr, (ConcurrencyLimitedModel, CooldownRetryModel)):
        curr = curr.wrapped
    return curr


def _is_already_bound_to_pool(model: Model | KnownModelName | str, pool_tag: str) -> bool:
    if isinstance(model, ConcurrencyLimitedModel) and isinstance(model.wrapped, CooldownRetryModel):
        return model.wrapped.pool_tag == pool_tag
    return False


class LlmConcurrencyController:
    """Runtime 级双池 LLM 并发协调器（Router 快车道池 + Role 共享池 + 全局 429 冷却门）。"""

    def __init__(
        self,
        *,
        role_max_concurrency: int = 4,
        router_max_concurrency: int = 2,
        max_queued: int | None = 128,
        rate_limit_max_retries: int = 3,
        base_delay_sec: float = 0.5,
    ) -> None:
        self._role_max: int = max(1, role_max_concurrency)
        self._router_max: int = max(1, router_max_concurrency)
        self._max_retries: int = max(0, rate_limit_max_retries)
        self._cooldown_gate: RateLimitCooldownGate = RateLimitCooldownGate(
            base_delay_sec=base_delay_sec
        )
        self._role_limiter: ConcurrencyLimiter = ConcurrencyLimiter.from_limit(
            ConcurrencyLimit(max_running=self._role_max, max_queued=max_queued),
            name="role-llm-pool",
        )
        self._router_limiter: ConcurrencyLimiter = ConcurrencyLimiter.from_limit(
            ConcurrencyLimit(max_running=self._router_max, max_queued=max_queued),
            name="router-llm-pool",
        )
        self._role_stats: ConcurrencyPoolStats = ConcurrencyPoolStats()
        self._router_stats: ConcurrencyPoolStats = ConcurrencyPoolStats()

    @property
    def cooldown_gate(self) -> RateLimitCooldownGate:
        return self._cooldown_gate

    @property
    def role_limiter(self) -> ConcurrencyLimiter:
        return self._role_limiter

    @property
    def router_limiter(self) -> ConcurrencyLimiter:
        return self._router_limiter

    @property
    def role_stats(self) -> ConcurrencyPoolStats:
        return self._role_stats

    @property
    def router_stats(self) -> ConcurrencyPoolStats:
        return self._router_stats

    def wrap_role_model(self, model: Model | KnownModelName | str) -> Model:
        """将 Role Agent 的模型幂等包裹进后台 Role 共享并发池（防重复嵌套死锁）。"""
        if _is_already_bound_to_pool(model, "role") and isinstance(model, Model):
            return model
        raw = _unwrap_raw_model(model)
        inner = CooldownRetryModel(
            raw,
            cooldown_gate=self._cooldown_gate,
            stats=self._role_stats,
            max_retries=self._max_retries,
            pool_tag="role",
        )
        return ConcurrencyLimitedModel(inner, limiter=self._role_limiter)

    def wrap_router_model(self, model: Model | KnownModelName | str) -> Model:
        """将前台 SuperAgentRouter 的模型幂等包裹进 Router 快车道并发池。"""
        if _is_already_bound_to_pool(model, "router") and isinstance(model, Model):
            return model
        raw = _unwrap_raw_model(model)
        inner = CooldownRetryModel(
            raw,
            cooldown_gate=self._cooldown_gate,
            stats=self._router_stats,
            max_retries=self._max_retries,
            pool_tag="router",
        )
        return ConcurrencyLimitedModel(inner, limiter=self._router_limiter)
