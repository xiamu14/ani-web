"""按工作区幂等懒编译与缓存 MCP 配置与权限策略（对齐 1118-extensions-spec）。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent.extensions.mcp_config import McpServerSpec, load_mcp_configs
from agent.permissions import (
    McpCompileReport,
    McpPolicyCompiler,
    McpToolPolicy,
    PermissionReviewer,
)


def _compute_prefixes(specs: tuple[McpServerSpec, ...]) -> tuple[str, ...]:
    prefixes: set[str] = set()
    for spec in specs:
        if spec.tool_prefix is False:
            continue
        if isinstance(spec.tool_prefix, str) and spec.tool_prefix:
            prefixes.add(spec.tool_prefix)
        else:
            prefixes.add(spec.name)
    return tuple(sorted(prefixes))


@dataclass(frozen=True, kw_only=True)
class WorkspaceMcpSnapshot:
    """单个工作区编译完成后的 MCP 只读快照。"""

    work_path: str | None
    specs: tuple[McpServerSpec, ...]
    policies: Mapping[str, McpToolPolicy]
    blocked_servers: frozenset[str]
    prefixes: tuple[str, ...]
    report: McpCompileReport


class WorkspaceMcpManager:
    """按规范化 work_path 幂等执行 OAuth 检查与增量 Jev 预编译，并同步给 Reviewer。"""

    def __init__(
        self,
        *,
        compiler: McpPolicyCompiler,
        reviewer: PermissionReviewer,
        global_mcp_path: Path | None = None,
    ) -> None:
        self._compiler: McpPolicyCompiler = compiler
        self._reviewer: PermissionReviewer = reviewer
        self._global_path: Path | None = global_mcp_path
        self._cache: dict[str, tuple[tuple[tuple[str, str], ...], WorkspaceMcpSnapshot]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._all_policies: dict[str, McpToolPolicy] = {}
        self._total_jev_calls: int = 0

    @property
    def total_jev_calls(self) -> int:
        return self._total_jev_calls

    def get_cached(self, work_path: str | Path | None = None) -> WorkspaceMcpSnapshot | None:
        key = str(Path(work_path).expanduser().resolve()) if work_path else ""
        hit = self._cache.get(key)
        return hit[1] if hit is not None else None

    async def ensure_compiled(
        self, work_path: str | Path | None = None
    ) -> WorkspaceMcpSnapshot:
        """若工作区 MCP 配置尚未编译或指纹已变化，则执行一次增量预编译并刷新缓存。"""
        ws_key = str(Path(work_path).expanduser().resolve()) if work_path else ""
        ws_root = Path(ws_key) if ws_key else None
        merged = load_mcp_configs(
            global_path=self._global_path, workspace_root=ws_root
        )
        fp = tuple((s.name, s.endpoint_fingerprint) for s in merged.servers)

        cached = self._cache.get(ws_key)
        if cached is not None and cached[0] == fp:
            return cached[1]

        lock = self._locks.setdefault(ws_key, asyncio.Lock())
        async with lock:
            merged = load_mcp_configs(
                global_path=self._global_path, workspace_root=ws_root
            )
            fp = tuple((s.name, s.endpoint_fingerprint) for s in merged.servers)
            cached = self._cache.get(ws_key)
            if cached is not None and cached[0] == fp:
                return cached[1]

            report = await self._compiler.compile_all(merged.servers)
            self._total_jev_calls += report.total_jev_calls
            policy_map = {p.prefixed_tool_name: p for p in report.policies}
            self._all_policies.update(policy_map)
            self._reviewer.set_mcp_policies(dict(self._all_policies))

            blocked = frozenset(
                e.server_name
                for e in report.entries
                if e.status in ("auth_required", "error")
            )
            snap = WorkspaceMcpSnapshot(
                work_path=ws_key or None,
                specs=merged.servers,
                policies=policy_map,
                blocked_servers=blocked,
                prefixes=_compute_prefixes(merged.servers),
                report=report,
            )
            self._cache[ws_key] = (fp, snap)
            return snap
