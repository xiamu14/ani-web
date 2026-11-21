"""MCP Toolset / Capability 构建工厂与短连接探测适配器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from fastmcp.client.elicitation import ElicitationHandler
from fastmcp.client.transports import StdioTransport
from pydantic_ai._http import AsyncHTTPClient
from pydantic_ai.capabilities import MCP, AbstractCapability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models import Model

from agent.extensions.mcp_auth import build_fastmcp_auth
from agent.extensions.mcp_config import McpServerSpec, compute_tool_schema_hash
from agent.permissions.mcp_ports import McpToolPolicy, ProbedMcpServer, ProbedMcpTool

type McpElicitationHandler = ElicitationHandler[object, object]


def build_mcp_toolset(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
    elicitation_handler: McpElicitationHandler | None = None,
    sampling_model: Model | None = None,
    http_client: AsyncHTTPClient | None = None,
) -> MCPToolset[object]:
    """根据 McpServerSpec 构建配置完整的 MCPToolset（支持 stdio/http/sse 与完整 Auth）。"""
    if spec.transport == "stdio":
        if not spec.command:
            raise ValueError(f"MCP stdio server {spec.name!r} missing `command`")
        transport = StdioTransport(
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env) if spec.env else None,
            cwd=spec.cwd,
        )
        return MCPToolset(
            transport,
            id=spec.name,
            include_instructions=spec.include_instructions,
            elicitation_handler=elicitation_handler,
            sampling_model=sampling_model,
        )

    if not spec.url:
        raise ValueError(f"MCP HTTP/SSE server {spec.name!r} missing `url`")
    auth = build_fastmcp_auth(spec, token_root=token_root, encryption_key=encryption_key)
    headers = dict(spec.headers) if spec.headers else None
    effective_http_client = http_client if headers is None else None
    return MCPToolset(
        spec.url,
        id=spec.name,
        auth=auth,
        headers=headers,
        verify=spec.verify,
        http_client=effective_http_client,
        include_instructions=spec.include_instructions,
        elicitation_handler=elicitation_handler,
        sampling_model=sampling_model,
    )


def build_mcp_capability(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
    elicitation_handler: McpElicitationHandler | None = None,
    sampling_model: Model | None = None,
    http_client: AsyncHTTPClient | None = None,
) -> MCP[object]:
    """将配置完整的 MCPToolset 包装为 `MCP(local=...)` Capability（支持 defer_loading/prefix/filter）。"""
    toolset = build_mcp_toolset(
        spec,
        token_root=token_root,
        encryption_key=encryption_key,
        elicitation_handler=elicitation_handler,
        sampling_model=sampling_model,
        http_client=http_client,
    )
    return MCP(
        local=toolset,
        id=spec.name,
        allowed_tools=list(spec.allowed_tools) if spec.allowed_tools is not None else None,
        tool_prefix=spec.tool_prefix,
        description=spec.description,
        include_instructions=spec.include_instructions,
        defer_loading=spec.defer_loading,
    )


def select_mcp_capabilities_for_role(
    role_name: str,
    specs: Sequence[McpServerSpec],
    *,
    policies: Mapping[str, McpToolPolicy] | None = None,
    blocked_servers: frozenset[str] = frozenset(),
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
    elicitation_handler: McpElicitationHandler | None = None,
    http_client: AsyncHTTPClient | None = None,
) -> list[AbstractCapability[object]]:
    """按 Role 过滤并懒装配 MCP Capabilities（显式 roles 优先，否则按 Jev recommended_roles）。"""
    if not specs:
        return []
    rec_by_server: dict[str, set[str]] = {}
    for policy in (policies or {}).values():
        rec_by_server.setdefault(policy.server_name, set()).update(policy.recommended_roles)

    caps: list[AbstractCapability[object]] = []
    for spec in specs:
        if spec.name in blocked_servers:
            continue
        if spec.roles is not None:
            if role_name not in spec.roles:
                continue
        elif spec.name in rec_by_server and role_name not in rec_by_server[spec.name]:
            continue
        caps.append(
            build_mcp_capability(
                spec,
                token_root=token_root,
                encryption_key=encryption_key,
                elicitation_handler=elicitation_handler,
                sampling_model=None,
                http_client=http_client,
            )
        )
    return caps


class FastMcpProbeAdapter:
    """实现 McpProbePort：短连接拉取 server_info 与 list_tools() 后立即关闭。"""

    def __init__(
        self,
        *,
        token_root: Path | None = None,
        encryption_key: str | bytes | None = None,
    ) -> None:
        self._token_root: Path | None = token_root
        self._encryption_key: str | bytes | None = encryption_key

    async def probe(self, spec: McpServerSpec) -> ProbedMcpServer:
        toolset = build_mcp_toolset(
            spec,
            token_root=self._token_root,
            encryption_key=self._encryption_key,
        )
        async with toolset:
            try:
                info = toolset.server_info
                impl_name = info.name
                impl_version = info.version
            except AttributeError:
                impl_name = spec.name
                impl_version = "0.0.0"

            raw_tools = await toolset.list_tools()

        allowed = set(spec.allowed_tools) if spec.allowed_tools is not None else None
        probed_tools: list[ProbedMcpTool] = []
        for tool in raw_tools:
            if allowed is not None and tool.name not in allowed:
                continue
            input_schema = (
                dict(cast(dict[str, object], tool.input_schema))
                if isinstance(tool.input_schema, dict)
                else {}
            )
            annotations: dict[str, object] = {}
            if tool.annotations is not None:
                annotations = cast(dict[str, object], tool.annotations.model_dump(exclude_none=True))
            desc = tool.description or ""
            schema_hash = compute_tool_schema_hash(
                server_name=spec.name,
                tool_name=tool.name,
                description=desc,
                input_schema=input_schema,
                annotations=annotations,
            )
            probed_tools.append(
                ProbedMcpTool(
                    name=tool.name,
                    description=desc,
                    input_schema=input_schema,
                    annotations=annotations,
                    schema_hash=schema_hash,
                )
            )

        return ProbedMcpServer(
            server_name=spec.name,
            endpoint_fingerprint=spec.endpoint_fingerprint,
            server_impl_name=impl_name,
            server_version=impl_version,
            tools=tuple(probed_tools),
        )
