"""Tools 渐进披露适配器：声明式延迟加载策略、中英双语检索策略与 ToolSearch 工厂。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, ToolSearch, ToolSearchStrategy
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, PreparedToolset

from agent.hooks.priority import HookPriority

_META_TOOL_KINDS: frozenset[str] = frozenset({"capability-load", "tool-search"})
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokenize_bilingual(text: str) -> set[str]:
    """提取英文词元（含下划线拆分）与 CJK 单字/二元组，用于中英双语工具检索。"""
    lowered = text.lower()
    tokens: set[str] = {m.group(0) for m in _ASCII_TOKEN_RE.finditer(lowered)}
    for part in lowered.split():
        cleaned = part.strip(".,:;!?()[]{}\"'`")
        if "_" in cleaned and all(ch.isalnum() or ch == "_" for ch in cleaned):
            tokens.add(cleaned)
    for match in _CJK_RUN_RE.finditer(lowered):
        run = match.group(0)
        if len(run) == 1:
            tokens.add(run)
        else:
            for idx in range(len(run) - 1):
                tokens.add(run[idx : idx + 2])
    return tokens


def bilingual_tool_search_fn(
    ctx: RunContext[object],
    queries: Sequence[str],
    tools: Sequence[ToolDefinition],
) -> list[str]:
    """兼容中英文查询的本地 ToolSearch 策略函数（工具名命中权重高于描述）。"""
    _ = ctx
    if not queries or not tools:
        return []

    scored: list[tuple[int, str]] = []
    query_items = [(q.strip().lower(), _tokenize_bilingual(q)) for q in queries if q.strip()]
    if not query_items:
        return []

    for tool_def in tools:
        name_lower = tool_def.name.lower()
        name_tokens = _tokenize_bilingual(tool_def.name)
        desc_tokens = _tokenize_bilingual(tool_def.description or "")
        score = 0
        for raw_q, q_tokens in query_items:
            if raw_q and raw_q in name_lower:
                score += 5
            score += 3 * len(q_tokens & name_tokens) + len(q_tokens & desc_tokens)
        if score > 0:
            scored.append((score, tool_def.name))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored]


@dataclass(frozen=True, kw_only=True)
class ToolDisclosurePolicy:
    """扩展工具渐进披露策略：控制常驻/延迟边界与检索策略。"""

    defer_names: frozenset[str] = frozenset()
    defer_patterns: tuple[str, ...] = ()
    eager_names: frozenset[str] = frozenset()
    strategy: ToolSearchStrategy[object] | None = None
    max_results: int = 10
    tool_description: str | None = None
    parameter_description: str | None = None

    def has_defer_rules(self) -> bool:
        """是否声明了按名或通配符自动延迟加载规则。"""
        return bool(self.defer_names or self.defer_patterns)

    def should_defer(self, tool_def: ToolDefinition) -> bool:
        """判定给定工具是否应被标记为延迟加载（常驻白名单与元工具永不延迟）。"""
        if tool_def.kind == "output" or tool_def.tool_kind in _META_TOOL_KINDS:
            return False
        if tool_def.name in self.eager_names:
            return False
        if tool_def.name in self.defer_names:
            return True
        return any(fnmatchcase(tool_def.name, pattern) for pattern in self.defer_patterns)


class DeferToolsCapability(AbstractCapability[object]):
    """基于 PreparedToolset 的声明式延迟标记能力（不侵入原有工具函数定义）。"""

    hook_priority: int = int(HookPriority.POLICY)

    def __init__(self, policy: ToolDisclosurePolicy) -> None:
        self._policy: ToolDisclosurePolicy = policy

    @override
    def get_wrapper_toolset(
        self, toolset: AbstractToolset[object]
    ) -> AbstractToolset[object]:
        async def _apply_defer(
            ctx: RunContext[object], tool_defs: list[ToolDefinition]
        ) -> list[ToolDefinition]:
            _ = ctx
            return [
                replace(td, defer_loading=True)
                if (not td.defer_loading and self._policy.should_defer(td))
                else td
                for td in tool_defs
            ]

        return PreparedToolset(toolset, _apply_defer)


def build_tool_search_capabilities(
    policy: ToolDisclosurePolicy | None = None,
) -> list[AbstractCapability[object]]:
    """按策略构建 Tools 渐进披露能力组合（可选 DeferToolsCapability + ToolSearch）。"""
    cfg = policy if policy is not None else ToolDisclosurePolicy()
    caps: list[AbstractCapability[object]] = []
    if cfg.has_defer_rules():
        caps.append(DeferToolsCapability(cfg))
    caps.append(
        ToolSearch[object](
            strategy=cfg.strategy,
            max_results=cfg.max_results,
            tool_description=cfg.tool_description,
            parameter_description=cfg.parameter_description,
        )
    )
    return caps
