"""OpenRouter TypeSafe Jev (/alpha/decisions) 通用结构化决策客户端（带并发信号量、连接池复用与 429/503 协同退避）。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

import httpx
import logfire

from agent.llm.rate_limiter import (
    ConcurrencyPoolStats,
    RateLimitCooldownGate,
    parse_retry_after_header,
)

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503, 504})


def build_decisions_url(base_url: str) -> str:
    """将 OPENROUTER_BASE_URL 补全为 /alpha/decisions 完整请求地址。"""
    cleaned = base_url.rstrip("/").removesuffix("/v1")
    return f"{cleaned}/alpha/decisions"


class JevHttpClient:
    """OpenRouter TypeSafe Jev (/alpha/decisions) 通用 HTTP 决策客户端。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api",
        model: str = "typesafe/jev-1.14",
        timeout_seconds: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
        max_concurrency: int = 3,
        max_retries: int = 3,
        base_delay_sec: float = 0.2,
        cooldown_gate: RateLimitCooldownGate | None = None,
        on_usage: Callable[[Mapping[str, object]], object] | None = None,
    ) -> None:
        self._api_key: str = api_key
        self._url: str = build_decisions_url(base_url)
        self._model: str = model
        self._timeout: float = timeout_seconds
        self._external_client: httpx.AsyncClient | None = http_client
        self._owned_client: httpx.AsyncClient | None = None
        self._max_concurrency: int = max(1, max_concurrency)
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self._max_concurrency)
        self._max_retries: int = max(0, max_retries)
        self._gate: RateLimitCooldownGate = (
            cooldown_gate
            if cooldown_gate is not None
            else RateLimitCooldownGate(base_delay_sec=base_delay_sec)
        )
        self._stats: ConcurrencyPoolStats = ConcurrencyPoolStats()
        self._on_usage: Callable[[Mapping[str, object]], object] | None = on_usage

    @property
    def decisions_url(self) -> str:
        return self._url

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def stats(self) -> ConcurrencyPoolStats:
        return self._stats

    @property
    def cooldown_gate(self) -> RateLimitCooldownGate:
        return self._gate

    def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None or self._owned_client.is_closed:
            limits = httpx.Limits(
                max_connections=max(8, self._max_concurrency * 2),
                max_keepalive_connections=self._max_concurrency,
            )
            self._owned_client = httpx.AsyncClient(timeout=self._timeout, limits=limits)
        return self._owned_client

    async def aclose(self) -> None:
        """关闭内部持有的持久化 AsyncClient 连接池（不关闭外部注入的 client）。"""
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()
            self._owned_client = None

    async def request_decisions(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, object],
        span_name: str = "jev.decision {model}",
        span_attrs: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """向 /alpha/decisions 发送结构化决策请求（受并发信号量与 429/503 协同退避保护）。"""
        payload = {"model": self._model, "state": dict(state), "questions": dict(questions)}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        client = self._get_client()

        async with self._semaphore:
            attempt = 0
            while True:
                await self._gate.wait_if_cooling_down()
                self._stats.on_enter()
                try:
                    with logfire.span(span_name, model=self._model, url=self._url) as span:
                        if span_attrs:
                            for key, val in span_attrs.items():
                                span.set_attribute(key, val)
                        resp = await client.post(
                            self._url,
                            json=payload,
                            headers=headers,
                            timeout=self._timeout,
                        )
                        if (
                            resp.status_code in _RETRYABLE_STATUS_CODES
                            and attempt < self._max_retries
                        ):
                            raw_hdr: object = resp.headers.get("Retry-After")  # pyright: ignore[reportAny]
                            retry_after = parse_retry_after_header(
                                raw_hdr if isinstance(raw_hdr, str) else None
                            )
                            self._stats.rate_limit_retries += 1
                            _ = self._gate.notify_rate_limited(
                                attempt, retry_after_sec=retry_after
                            )
                            attempt += 1
                            continue
                        _ = resp.raise_for_status()
                        data = cast(dict[str, object], resp.json())
                        span.set_attribute("response_model", data.get("model"))
                        span.set_attribute("usage", data.get("usage"))
                        span.set_attribute("answers", data.get("answers"))
                        raw_usage = data.get("usage")
                        if self._on_usage is not None and isinstance(raw_usage, Mapping):
                            maybe = self._on_usage(cast(Mapping[str, object], raw_usage))
                            if inspect.isawaitable(maybe):
                                _ = await cast(Awaitable[object], maybe)
                        return data
                finally:
                    self._stats.on_exit()
