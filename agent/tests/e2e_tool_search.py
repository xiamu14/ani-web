"""真实 LLM + Tavily 网络搜索 E2E 验证：WebSearch / WebFetch 延迟加载、检索激活与 KV Cache 观测。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import warnings
from dataclasses import dataclass, field, replace
from html import unescape
from pathlib import Path
from typing import cast, override

import httpx
import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability, WebFetch, WebSearch
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.workspaces import LocalWorkspaceBackend

from agent.config import load_settings
from agent.config.settings import locate_env_file
from agent.core import build_default_role_registry, build_model, configure_observability
from agent.extensions import ToolDisclosurePolicy, bilingual_tool_search_fn
from agent.hooks import HookPriority, PermissionGuardHook
from agent.permissions import PermissionReviewer
from agent.tools import ToolExecutionQueueHook

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def _build_tavily_search_tool(api_key: str) -> Tool[object]:
    async def web_search(query: str, max_results: int = 5) -> str:
        """Search the public web via Tavily API and return answer plus relevant URLs and snippets."""
        clamped = min(max(max_results, 1), 8)
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": clamped,
                    "include_answer": True,
                },
            )
            _ = resp.raise_for_status()
            data = cast(dict[str, object], resp.json())

        lines: list[str] = []
        answer = data.get("answer")
        if isinstance(answer, str) and answer.strip():
            lines.append(f"Answer: {answer.strip()}")
        results = data.get("results")
        if isinstance(results, list):
            for idx, item in enumerate(cast(list[object], results), start=1):
                if not isinstance(item, dict):
                    continue
                entry = cast(dict[str, object], item)
                title = str(entry.get("title", "")).strip()
                url = str(entry.get("url", "")).strip()
                snippet = str(entry.get("content", "")).strip()[:400]
                lines.append(f"[{idx}] {title}\nURL: {url}\nSnippet: {snippet}")
        return "\n\n".join(lines) if lines else "No search results found."

    return Tool(web_search, name="web_search", takes_ctx=False)


def _build_web_fetch_tool() -> Tool[object]:
    async def web_fetch(url: str, max_chars: int = 5000) -> str:
        """Fetch and extract readable plain text content from a public HTTP/HTTPS URL."""
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AgentResearcher/1.0)"}
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                _ = resp.raise_for_status()
                raw = resp.text
        except httpx.HTTPError as exc:
            return f"Fetch failed for {url}: {exc}. Please try another URL from the search results."
        cleaned = _SCRIPT_STYLE_RE.sub(" ", raw)
        cleaned = _TAG_RE.sub(" ", cleaned)
        text = _WS_RE.sub(" ", unescape(cleaned)).strip()
        return text[: max(max_chars, 500)] if text else "(empty page)"

    return Tool(web_fetch, name="web_fetch", takes_ctx=False)


@dataclass(frozen=True, kw_only=True)
class TurnRecord:
    """Single turn tool visibility and KV cache usage telemetry."""

    turn: int
    visible_tools: tuple[str, ...]
    instr_sha: str
    prefix_stable: bool
    tool_calls: tuple[str, ...]
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int


@dataclass
class ToolSearchTelemetryHook(AbstractCapability[object]):
    """观测 ToolSearch 每轮可见工具变化、实际调用轨迹与 KV Cache 指标。"""

    hook_priority: int = int(HookPriority.OBSERVABILITY)
    turns: list[TurnRecord] = field(default_factory=list)
    called_tools: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    _baseline_sha: str | None = field(default=None, init=False, repr=False)
    _pending: tuple[int, tuple[str, ...], str, bool] | None = field(
        default=None, init=False, repr=False
    )

    @override
    async def before_model_request(
        self, ctx: RunContext[object], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        _ = ctx
        msgs = request_context.messages
        last_req = next((m for m in reversed(msgs) if isinstance(m, ModelRequest)), None)
        instr = (last_req.instructions or "") if last_req is not None else ""
        sha = hashlib.sha256(instr.encode("utf-8")).hexdigest()[:12]
        if self._baseline_sha is None:
            self._baseline_sha = sha
        params = request_context.model_request_parameters
        revealed = set(params.revealed_tool_names)
        visible = tuple(
            t.name for t in params.function_tools if not t.defer_loading or t.name in revealed
        )
        self._pending = (len(self.turns), visible, sha, sha == self._baseline_sha)
        return request_context

    @override
    async def after_model_request(
        self,
        ctx: RunContext[object],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        _ = (ctx, request_context)
        pending = self._pending
        assert pending is not None
        turn_idx, visible, sha, stable = pending

        calls: list[str] = []
        for part in response.parts:
            if isinstance(part, ToolCallPart):
                args_dict = cast(dict[str, object], part.args_as_dict())
                self.called_tools.append((part.tool_name, args_dict))
                calls.append(f"{part.tool_name}({args_dict})")

        usage = response.usage
        self.turns.append(
            TurnRecord(
                turn=turn_idx,
                visible_tools=visible,
                instr_sha=sha,
                prefix_stable=stable,
                tool_calls=tuple(calls),
                input_tokens=usage.input_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                output_tokens=usage.output_tokens,
            )
        )
        return response


async def run_e2e() -> None:
    _ = os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    _ = load_dotenv(locate_env_file())
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    assert tavily_key, "TAVILY_API_KEY is required in .env for real WebSearch E2E."

    settings = load_settings()
    logfire_enabled = configure_observability(settings)
    model = build_model(settings)

    base_researcher = build_default_role_registry(model).get("researcher")
    policy = ToolDisclosurePolicy(
        defer_names=frozenset({"web_search", "web_fetch"}),
        eager_names=frozenset(
            {"read_file", "list_directory", "search_files", "find_files", "file_info"}
        ),
        strategy=bilingual_tool_search_fn,
    )
    researcher_role = replace(base_researcher, tool_disclosure=policy)

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir()
        prompt = (
            "请帮我调研 Pydantic AI 框架官方文档中关于 ToolSearch 或 Capabilities 延迟加载的说明：\n"
            "1. 先使用网络搜索工具查找相关官方页面或最新资料；\n"
            "2. 再抓取其中一个官方结果页面的正文内容进行核实；\n"
            "3. 用中文简洁总结其核心机制，并附上你访问的真实 URL。"
        )
        perm_hook = PermissionGuardHook(
            reviewer=PermissionReviewer(),
            workspace_root=ws,
            user_intent=prompt,
        )
        queue_hook = ToolExecutionQueueHook(workspace_root=ws)
        telemetry = ToolSearchTelemetryHook()
        web_search_cap = WebSearch(native=False, local=_build_tavily_search_tool(tavily_key))
        web_fetch_cap = WebFetch(native=False, local=_build_web_fetch_tool())

        agent: Agent[object, str] = researcher_role.build_agent(
            extra_capabilities=[perm_hook, queue_hook, telemetry, web_search_cap, web_fetch_cap],
        )

        try:
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = await agent.run(prompt, workspace=LocalWorkspaceBackend(ws))
        finally:
            perm_hook.release_runs()
            if logfire_enabled:
                _ = logfire.force_flush()

        print("\n=== ToolSearch + WebSearch (Tavily) + WebFetch E2E 观测报告 ===")
        print(f"Model: {settings.model_name}")
        for t in telemetry.turns:
            hit_pct = (t.cache_read_tokens / t.input_tokens * 100.0) if t.input_tokens > 0 else 0.0
            print(
                f"Turn {t.turn} | Stable={t.prefix_stable} ({t.instr_sha}) | "
                + f"In={t.input_tokens} CacheRead={t.cache_read_tokens} ({hit_pct:.1f}%) Out={t.output_tokens}"
            )
            print(f"  VisibleTools: {list(t.visible_tools)}")
            print(f"  ToolCalls:    {list(t.tool_calls) or ['(final_text)']}")

        print(f"\n=== Final Output ===\n{result.output}")

        # 1) Turn 0 仅暴露 search_tools + 常驻只读工具，web_search 与 web_fetch 隐藏
        assert telemetry.turns, "No turns recorded"
        t0_tools = set(telemetry.turns[0].visible_tools)
        assert "search_tools" in t0_tools, f"search_tools missing in turn 0: {t0_tools}"
        assert "web_search" not in t0_tools, f"web_search leaked in turn 0: {t0_tools}"
        assert "web_fetch" not in t0_tools, f"web_fetch leaked in turn 0: {t0_tools}"

        # 2) 模型调用 search_tools 激活后，真实调用 web_search（Tavily）和 web_fetch
        called_names = [name for name, _ in telemetry.called_tools]
        assert "search_tools" in called_names, f"search_tools never called: {called_names}"
        assert "web_search" in called_names, f"web_search never called: {called_names}"
        assert "web_fetch" in called_names, f"web_fetch never called: {called_names}"

        # 3) Prefix Cache 稳定与最终输出带真实 URL
        assert all(t.prefix_stable for t in telemetry.turns), "Instructions prefix mutated!"
        assert "https://" in result.output or "http://" in result.output
        print("\nTOOL SEARCH WEB E2E ASSERTIONS PASSED (all checks OK).")


if __name__ == "__main__":
    asyncio.run(run_e2e())
