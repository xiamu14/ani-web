"""MCP 工具增量预编译器与 Jev 静态分类适配器（通过 Port 解耦 Probe / Classifier / Store）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

import httpx
import logfire

from agent.llm.jev_client import JevHttpClient

if TYPE_CHECKING:
    from agent.extensions.mcp_config import McpServerSpec
from agent.permissions.mcp_ports import (
    LockMode,
    McpAuthReadinessPort,
    McpClassifiedDecision,
    McpClassifierPort,
    McpCompileEntry,
    McpCompileReport,
    McpPolicyStorePort,
    McpProbePort,
    McpToolPolicy,
    ProbedMcpServer,
    ProbedMcpTool,
)
from agent.permissions.rules import RuleLevel

_VALID_LEVELS: frozenset[str] = frozenset({"pass", "review", "forbid"})
_VALID_LOCK_MODES: frozenset[str] = frozenset({"shared", "exclusive"})
_VALID_ROLES: frozenset[str] = frozenset({"researcher", "coder", "tester", "reviewer"})


def compute_prefixed_tool_name(spec: McpServerSpec, tool_name: str) -> str:
    """按 `MCP.get_toolset()` 前缀规则计算运行期可见工具名。"""
    prefix = spec.name if spec.tool_prefix is True else spec.tool_prefix
    if isinstance(prefix, str) and prefix:
        return f"{prefix}_{tool_name}"
    return tool_name


def _fallback_decision(tool_name: str, reason: str) -> McpClassifiedDecision:
    return McpClassifiedDecision(
        tool_name=tool_name,
        level="review",
        lock_mode="exclusive",
        recommended_roles=("coder",),
        reason=reason,
    )


def _normalize_roles(raw_roles: object) -> tuple[str, ...]:
    if not isinstance(raw_roles, list):
        return ("coder",)
    cleaned = tuple(str(r).strip() for r in raw_roles if str(r).strip() in _VALID_ROLES)
    return cleaned or ("coder",)


class JevMcpClassifier:
    """使用 Jev (/alpha/decisions) 对增量 MCP 工具执行一次性批量静态定级。"""

    def __init__(self, jev_client: JevHttpClient) -> None:
        self._client: JevHttpClient = jev_client

    async def classify(
        self,
        server: ProbedMcpServer,
        tools: Sequence[ProbedMcpTool],
    ) -> Sequence[McpClassifiedDecision]:
        if not tools:
            return []

        state: dict[str, object] = {
            "server_name": server.server_name,
            "server_impl": f"{server.server_impl_name}@{server.server_version}",
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "annotations": tool.annotations,
                }
                for tool in tools
            ],
        }
        questions: dict[str, object] = {}
        for tool in tools:
            questions[f"{tool.name}__level"] = {
                "type": "choice",
                "instructions": (
                    f"Classify permission level for MCP tool `{tool.name}` ({tool.description})."
                ),
                "criteria": {
                    "pass": "Strictly read-only query/search with no side effects and no sensitive credential export.",
                    "review": "External write, state mutation, quota-consuming action, or sensitive data read.",
                    "forbid": "Destructive deletion, credential exfiltration, or dangerous system action.",
                },
            }
            questions[f"{tool.name}__lock_mode"] = {
                "type": "choice",
                "instructions": f"Classify concurrency lock_mode for MCP tool `{tool.name}`.",
                "criteria": {
                    "shared": "Stateless read-only query safe to run concurrently across roles.",
                    "exclusive": "Mutating or stateful operation requiring exclusive serialization.",
                },
            }
            questions[f"{tool.name}__role_group"] = {
                "type": "choice",
                "instructions": f"Assign suitable roles for MCP tool `{tool.name}`.",
                "criteria": {
                    "readonly_all": "Read-only research/query tool suitable for researcher, reviewer, coder, tester.",
                    "dev_only": "Mutating/build/write tool suitable only for coder and tester.",
                    "admin_none": "Forbidden or unsafe tool; assign to no role.",
                },
            }
        try:
            resp = await self._client.request_decisions(
                state=state,
                questions=questions,
                span_name="jev.mcp_compile {model}",
                span_attrs={"mcp.server_name": server.server_name, "mcp.tool_count": len(tools)},
            )
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            logfire.warning(
                "Jev MCP classification failed; falling back to review+exclusive",
                server_name=server.server_name,
                error=str(exc),
            )
            return [_fallback_decision(t.name, f"Jev fallback: {exc}") for t in tools]

        answers_raw = resp.get("answers", resp.get("decisions", resp))
        answers_map = cast(dict[str, object], answers_raw) if isinstance(answers_raw, dict) else {}
        role_group_map: dict[str, tuple[str, ...]] = {
            "readonly_all": ("researcher", "reviewer", "coder", "tester"),
            "dev_only": ("coder", "tester"),
            "admin_none": (),
        }

        def _extract_choice(key: str) -> str | None:
            val = answers_map.get(key)
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, dict):
                choice = cast(dict[str, object], val).get("choice")
                if isinstance(choice, str):
                    return choice.strip()
            return None

        results: list[McpClassifiedDecision] = []
        for tool in tools:
            raw_level = _extract_choice(f"{tool.name}__level")
            raw_lock = _extract_choice(f"{tool.name}__lock_mode") or "exclusive"
            raw_role_group = _extract_choice(f"{tool.name}__role_group") or "dev_only"
            if raw_level is None:
                results.append(_fallback_decision(tool.name, "Jev omitted tool decision"))
                continue
            level: RuleLevel = cast(RuleLevel, raw_level) if raw_level in _VALID_LEVELS else "review"
            lock_mode: LockMode = (
                cast(LockMode, raw_lock) if raw_lock in _VALID_LOCK_MODES else "exclusive"
            )
            roles = role_group_map.get(raw_role_group, ("coder",))
            results.append(
                McpClassifiedDecision(
                    tool_name=tool.name,
                    level=level,
                    lock_mode=lock_mode,
                    recommended_roles=roles,
                    reason=f"Jev classified: level={level}, lock={lock_mode}, roles={raw_role_group}",
                )
            )
        return results


class McpPolicyCompiler:
    """MCP 增量权限预编译器：仅对新增或 Schema/版本漂移的工具调用 Classifier。"""

    def __init__(
        self,
        *,
        probe: McpProbePort,
        classifier: McpClassifierPort,
        store: McpPolicyStorePort,
        auth_readiness: McpAuthReadinessPort | None = None,
    ) -> None:
        self._probe: McpProbePort = probe
        self._classifier: McpClassifierPort = classifier
        self._store: McpPolicyStorePort = store
        self._auth_readiness: McpAuthReadinessPort | None = auth_readiness

    async def compile_server(self, spec: McpServerSpec) -> McpCompileEntry:
        if self._auth_readiness is not None and not await self._auth_readiness.is_ready(spec):
            existing = await self._store.list_by_server(spec.name)
            return McpCompileEntry(
                server_name=spec.name,
                endpoint_fingerprint=spec.endpoint_fingerprint,
                status="auth_required",
                policies=tuple(existing),
                jev_calls=0,
                error="OAuth token not present locally; run interactive CLI auth first.",
            )

        try:
            probed = await self._probe.probe(spec)
        except (OSError, TimeoutError, ConnectionError, RuntimeError, ValueError) as exc:
            return McpCompileEntry(
                server_name=spec.name,
                endpoint_fingerprint=spec.endpoint_fingerprint,
                status="error",
                policies=(),
                jev_calls=0,
                error=str(exc),
            )

        existing_rows = await self._store.list_by_server(spec.name)
        existing_by_tool = {row.tool_name: row for row in existing_rows}

        reused: dict[str, McpToolPolicy] = {}
        delta_tools: list[ProbedMcpTool] = []
        for tool in probed.tools:
            prefixed = compute_prefixed_tool_name(spec, tool.name)
            cached = existing_by_tool.get(tool.name)
            if (
                cached is not None
                and cached.endpoint_fingerprint == probed.endpoint_fingerprint
                and cached.server_version == probed.server_version
                and cached.schema_hash == tool.schema_hash
                and cached.prefixed_tool_name == prefixed
            ):
                reused[tool.name] = cached
            else:
                delta_tools.append(tool)

        jev_calls = 0
        classified_by_tool: dict[str, McpClassifiedDecision] = {}
        if delta_tools:
            jev_calls = 1
            decisions = await self._classifier.classify(probed, delta_tools)
            classified_by_tool = {d.tool_name: d for d in decisions}

        final_policies: list[McpToolPolicy] = []
        for tool in probed.tools:
            if tool.name in reused:
                final_policies.append(reused[tool.name])
                continue
            decision = classified_by_tool.get(tool.name) or _fallback_decision(
                tool.name, "Classifier returned no entry"
            )
            final_policies.append(
                McpToolPolicy(
                    server_name=spec.name,
                    endpoint_fingerprint=probed.endpoint_fingerprint,
                    server_impl_name=probed.server_impl_name,
                    server_version=probed.server_version,
                    tool_name=tool.name,
                    prefixed_tool_name=compute_prefixed_tool_name(spec, tool.name),
                    schema_hash=tool.schema_hash,
                    level=decision.level,
                    lock_mode=decision.lock_mode,
                    recommended_roles=decision.recommended_roles,
                    reason=decision.reason,
                )
            )

        needs_store_sync = bool(delta_tools) or (len(existing_rows) != len(final_policies))
        if needs_store_sync:
            await self._store.replace_server_policies(spec.name, final_policies)

        return McpCompileEntry(
            server_name=spec.name,
            endpoint_fingerprint=probed.endpoint_fingerprint,
            status="compiled" if delta_tools else "cached",
            policies=tuple(final_policies),
            jev_calls=jev_calls,
        )

    async def compile_all(self, specs: Sequence[McpServerSpec]) -> McpCompileReport:
        entries: list[McpCompileEntry] = []
        all_policies: list[McpToolPolicy] = []
        total_jev_calls = 0
        for spec in specs:
            entry = await self.compile_server(spec)
            entries.append(entry)
            all_policies.extend(entry.policies)
            total_jev_calls += entry.jev_calls
        return McpCompileReport(
            entries=tuple(entries),
            policies=tuple(all_policies),
            total_jev_calls=total_jev_calls,
        )
