"""OpenRouter TypeSafe Jev (/alpha/decisions) 通用结构化决策客户端。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import httpx
import logfire


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
    ) -> None:
        self._api_key: str = api_key
        self._url: str = build_decisions_url(base_url)
        self._model: str = model
        self._timeout: float = timeout_seconds
        self._external_client: httpx.AsyncClient | None = http_client

    @property
    def decisions_url(self) -> str:
        return self._url

    @property
    def model_name(self) -> str:
        return self._model

    async def request_decisions(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, object],
        span_name: str = "jev.decision {model}",
        span_attrs: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """向 /alpha/decisions 发送结构化决策请求并返回完整 JSON 响应。"""
        payload = {"model": self._model, "state": dict(state), "questions": dict(questions)}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        with logfire.span(span_name, model=self._model, url=self._url) as span:
            if span_attrs:
                for key, val in span_attrs.items():
                    span.set_attribute(key, val)
            if self._external_client is not None:
                resp = await self._external_client.post(
                    self._url, json=payload, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self._url, json=payload, headers=headers)
            _ = resp.raise_for_status()
            data = cast(dict[str, object], resp.json())
            span.set_attribute("response_model", data.get("model"))
            span.set_attribute("usage", data.get("usage"))
            span.set_attribute("answers", data.get("answers"))
            return data
