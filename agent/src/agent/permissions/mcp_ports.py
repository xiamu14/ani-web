"""MCP 预编译与运行期策略解耦端口（零 IO / 零 DB / 零 HTTP 实现）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from agent.permissions.rules import RuleLevel

if TYPE_CHECKING:
    from agent.extensions.mcp_config import McpServerSpec

LockMode = Literal["exclusive", "shared", "none"]
McpCompileStatus = Literal["compiled", "cached", "auth_required", "error"]


@dataclass(frozen=True, kw_only=True)
class ProbedMcpTool:
    """从 MCP Server 探测到的单个工具元数据。"""

    name: str
    description: str
    input_schema: dict[str, object]
    annotations: dict[str, object]
    schema_hash: str


@dataclass(frozen=True, kw_only=True)
class ProbedMcpServer:
    """MCP Server 短连接探测结果。"""

    server_name: str
    endpoint_fingerprint: str
    server_impl_name: str
    server_version: str
    tools: tuple[ProbedMcpTool, ...]


@dataclass(frozen=True, kw_only=True)
class McpClassifiedDecision:
    """分类器（如 Jev）对单个 MCP 工具的静态定级结果。"""

    tool_name: str
    level: RuleLevel
    lock_mode: LockMode
    recommended_roles: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class McpToolPolicy:
    """落盘并供运行期 O(1) 查表的 MCP 工具权限策略。"""

    server_name: str
    endpoint_fingerprint: str
    server_impl_name: str
    server_version: str
    tool_name: str
    prefixed_tool_name: str
    schema_hash: str
    level: RuleLevel
    lock_mode: LockMode
    recommended_roles: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class McpCompileEntry:
    """单个 MCP Server 的增量预编译结果摘要。"""

    server_name: str
    endpoint_fingerprint: str
    status: McpCompileStatus
    policies: tuple[McpToolPolicy, ...] = ()
    jev_calls: int = 0
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class McpCompileReport:
    """整批 mcpServers 预编译汇总结果。"""

    entries: tuple[McpCompileEntry, ...]
    policies: tuple[McpToolPolicy, ...]
    total_jev_calls: int


class McpProbePort(Protocol):
    """MCP Server 探测端口：建立短暂连接获取 server_info 与工具列表后关闭。"""

    async def probe(self, spec: McpServerSpec) -> ProbedMcpServer: ...


class McpClassifierPort(Protocol):
    """MCP 工具静态分类端口（如 Jev 适配器）。"""

    async def classify(
        self,
        server: ProbedMcpServer,
        tools: Sequence[ProbedMcpTool],
    ) -> Sequence[McpClassifiedDecision]: ...


class McpPolicyStorePort(Protocol):
    """MCP 工具策略持久化仓储端口。"""

    async def list_by_server(self, server_name: str) -> list[McpToolPolicy]: ...

    async def list_all(self) -> list[McpToolPolicy]: ...

    async def replace_server_policies(
        self,
        server_name: str,
        policies: Sequence[McpToolPolicy],
    ) -> None: ...


class McpAuthReadinessPort(Protocol):
    """OAuth 本地凭据就绪检查端口，用于避免无头预编译阻塞在浏览器登录。"""

    async def is_ready(self, spec: McpServerSpec) -> bool: ...
