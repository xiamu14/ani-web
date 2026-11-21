"""MCP 配置纯解析器: 支持两级 mcp.json 合并、环境变量展开、脱敏端点指纹与 Schema 哈希（零 IO / 零网络）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse, urlunparse

McpScope = Literal["global", "workspace"]
McpTransportType = Literal["stdio", "http", "sse"]
McpAuthType = Literal["none", "bearer", "oauth", "oauth_client_credentials", "oauth_private_key_jwt"]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_VALID_AUTH_TYPES: frozenset[str] = frozenset(
    {"none", "bearer", "oauth", "oauth_client_credentials", "oauth_private_key_jwt"}
)


@dataclass(frozen=True, kw_only=True)
class McpAuthSpec:
    """MCP Server 认证配置（纯数据结构）。"""

    type: McpAuthType = "none"
    token: str | None = None
    scopes: tuple[str, ...] = ()
    client_id: str | None = None
    client_secret: str | None = None
    callback_port: int | None = None
    client_metadata_url: str | None = None
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] = "client_secret_basic"
    issuer: str | None = None


@dataclass(frozen=True, kw_only=True)
class McpServerSpec:
    """单个规范化后的 MCP Server 配置。"""

    name: str
    scope: McpScope
    transport: McpTransportType
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    verify: bool | str = True
    auth: McpAuthSpec = field(default_factory=McpAuthSpec)
    roles: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] | None = None
    tool_prefix: str | bool | None = None
    defer_loading: bool = False
    description: str | None = None
    include_instructions: bool = True
    endpoint_fingerprint: str = ""


@dataclass(frozen=True, kw_only=True)
class McpConfigIssue:
    """配置解析或去重告警。"""

    server_name: str
    scope: McpScope
    reason: str


@dataclass(frozen=True, kw_only=True)
class McpMergedConfig:
    """两级合并与去重后的 MCP 配置集合。"""

    servers: tuple[McpServerSpec, ...]
    overridden_names: tuple[str, ...] = ()
    deduplicated_names: tuple[str, ...] = ()
    issues: tuple[McpConfigIssue, ...] = ()


def expand_env_vars(value: object, environ: Mapping[str, str] | None = None) -> object:
    """递归展开字符串中的 `${VAR}` 与 `${VAR:-default}`，缺失且无默认值时抛出 ValueError。"""
    env_map = os.environ if environ is None else environ
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            resolved = env_map.get(var_name)
            if resolved is not None and resolved != "":
                return resolved
            if default_val is not None:
                return default_val
            raise ValueError(f"Missing environment variable in mcp.json: {var_name}")

        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, list):
        return [expand_env_vars(item, env_map) for item in value]
    if isinstance(value, dict):
        return {str(k): expand_env_vars(v, env_map) for k, v in value.items()}
    return value


def _canonical_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def compute_endpoint_fingerprint(
    *,
    transport: McpTransportType,
    command: str | None,
    args: tuple[str, ...],
    url: str | None,
    auth: McpAuthSpec,
) -> str:
    """计算脱敏的端点指纹。

    严格排除 token、client_secret、Authorization header、env 值等动态/敏感凭据，
    确保 Token 刷新或密钥轮换不会误触发 mcp_tool_policies 全表失效。
    """
    payload: dict[str, object] = {
        "transport": transport,
        "auth_type": auth.type,
        "client_id": auth.client_id or "",
        "issuer": auth.issuer or "",
    }
    if transport == "stdio":
        payload["command"] = (command or "").strip()
        payload["args"] = list(args)
    else:
        payload["url"] = _canonical_url(url or "")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_tool_schema_hash(
    *,
    server_name: str,
    tool_name: str,
    description: str | None,
    input_schema: Mapping[str, object] | None,
    annotations: Mapping[str, object] | None = None,
) -> str:
    """计算单个 MCP Tool 的结构化 Schema 指纹，用于检测工具变更与版本漂移。"""
    payload = {
        "server_name": server_name,
        "tool_name": tool_name,
        "description": (description or "").strip(),
        "input_schema": dict(input_schema or {}),
        "annotations": dict(annotations or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_str_tuple(raw: object, *, field_name: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return tuple(parts)
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    raise ValueError(f"Invalid `{field_name}` in mcp.json: expected list[str] or str")


def _parse_auth(raw_auth: object) -> McpAuthSpec:
    if raw_auth is None:
        return McpAuthSpec()
    if isinstance(raw_auth, str):
        if raw_auth == "oauth":
            return McpAuthSpec(type="oauth")
        return McpAuthSpec(type="bearer", token=raw_auth)
    if not isinstance(raw_auth, dict):
        raise TypeError("Invalid `auth` in mcp.json: expected object or string")
    auth_map = cast(dict[str, object], raw_auth)
    auth_type = str(auth_map.get("type", "none")).strip()
    if auth_type not in _VALID_AUTH_TYPES:
        raise ValueError(f"Unsupported MCP auth type: {auth_type!r}")
    typed_auth = cast(McpAuthType, auth_type)
    scopes = _parse_str_tuple(auth_map.get("scopes"), field_name="auth.scopes") or ()
    callback_raw = auth_map.get("callback_port")
    callback_port = int(callback_raw) if isinstance(callback_raw, int) else None
    method_raw = str(auth_map.get("token_endpoint_auth_method", "client_secret_basic"))
    token_method: Literal["client_secret_basic", "client_secret_post"] = (
        "client_secret_post" if method_raw == "client_secret_post" else "client_secret_basic"
    )
    return McpAuthSpec(
        type=typed_auth,
        token=str(auth_map["token"]) if auth_map.get("token") is not None else None,
        scopes=scopes,
        client_id=str(auth_map["client_id"]) if auth_map.get("client_id") is not None else None,
        client_secret=str(auth_map["client_secret"]) if auth_map.get("client_secret") is not None else None,
        callback_port=callback_port,
        client_metadata_url=(
            str(auth_map["client_metadata_url"]) if auth_map.get("client_metadata_url") is not None else None
        ),
        token_endpoint_auth_method=token_method,
        issuer=str(auth_map["issuer"]) if auth_map.get("issuer") is not None else None,
    )


def _infer_http_transport(url: str, raw_transport: object) -> McpTransportType:
    if raw_transport in ("http", "sse"):
        return cast(McpTransportType, raw_transport)
    path = urlparse(url).path.rstrip("/").lower()
    return "sse" if path.endswith("/sse") else "http"


def _parse_server_entry(
    name: str,
    raw_entry: Mapping[str, object],
    *,
    scope: McpScope,
    environ: Mapping[str, str],
) -> McpServerSpec:
    expanded = cast(dict[str, object], expand_env_vars(dict(raw_entry), environ))
    roles = _parse_str_tuple(expanded.get("roles"), field_name="roles")
    allowed_tools = _parse_str_tuple(expanded.get("allowed_tools"), field_name="allowed_tools")
    prefix_raw = expanded.get("tool_prefix")
    tool_prefix: str | bool | None = prefix_raw if isinstance(prefix_raw, (str, bool)) else name
    defer_loading = bool(expanded.get("defer_loading", False))
    description = str(expanded["description"]) if expanded.get("description") is not None else None
    include_instructions = bool(expanded.get("include_instructions", True))

    if "command" in expanded and expanded["command"] is not None:
        command = str(expanded["command"]).strip()
        args = _parse_str_tuple(expanded.get("args"), field_name="args") or ()
        raw_env = expanded.get("env")
        env_dict = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
        cwd = str(expanded["cwd"]) if expanded.get("cwd") is not None else None
        auth = McpAuthSpec(type="none")
        fingerprint = compute_endpoint_fingerprint(
            transport="stdio", command=command, args=args, url=None, auth=auth
        )
        return McpServerSpec(
            name=name,
            scope=scope,
            transport="stdio",
            command=command,
            args=args,
            env=env_dict,
            cwd=cwd,
            auth=auth,
            roles=roles,
            allowed_tools=allowed_tools,
            tool_prefix=tool_prefix,
            defer_loading=defer_loading,
            description=description,
            include_instructions=include_instructions,
            endpoint_fingerprint=fingerprint,
        )

    if "url" in expanded and expanded["url"] is not None:
        url = str(expanded["url"]).strip()
        transport = _infer_http_transport(url, expanded.get("transport") or expanded.get("type"))
        raw_headers = expanded.get("headers")
        headers = {str(k): str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
        verify_raw = expanded.get("verify", True)
        verify: bool | str = verify_raw if isinstance(verify_raw, (bool, str)) else True
        auth = _parse_auth(expanded.get("auth"))
        fingerprint = compute_endpoint_fingerprint(
            transport=transport, command=None, args=(), url=url, auth=auth
        )
        return McpServerSpec(
            name=name,
            scope=scope,
            transport=transport,
            url=url,
            headers=headers,
            verify=verify,
            auth=auth,
            roles=roles,
            allowed_tools=allowed_tools,
            tool_prefix=tool_prefix,
            defer_loading=defer_loading,
            description=description,
            include_instructions=include_instructions,
            endpoint_fingerprint=fingerprint,
        )

    raise ValueError(f"MCP server {name!r} must define either `command` or `url`")


def _extract_servers_map(raw_config: Mapping[str, object] | None) -> Mapping[str, Mapping[str, object]]:
    if not raw_config:
        return {}
    servers = raw_config.get("mcpServers")
    if servers is None:
        servers = raw_config.get("servers")
    if servers is None:
        return {}
    if not isinstance(servers, Mapping):
        raise TypeError("`mcpServers` / `servers` must be a JSON object")
    return {
        str(name): cast(Mapping[str, object], entry)
        for name, entry in servers.items()
        if isinstance(entry, Mapping)
    }


def merge_mcp_configs(
    *,
    global_config: Mapping[str, object] | None = None,
    workspace_config: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> McpMergedConfig:
    """合并全局与工作区 mcp.json：按 server_name 整条覆盖，再按 endpoint_fingerprint 同源去重。"""
    env_map = os.environ if environ is None else environ
    parsed_by_name: dict[str, McpServerSpec] = {}
    issues: list[McpConfigIssue] = []
    overridden: list[str] = []

    for scope, cfg in (("global", global_config), ("workspace", workspace_config)):
        typed_scope = cast(McpScope, scope)
        for name, raw_entry in _extract_servers_map(cfg).items():
            try:
                spec = _parse_server_entry(name, raw_entry, scope=typed_scope, environ=env_map)
            except (ValueError, TypeError) as exc:
                issues.append(McpConfigIssue(server_name=name, scope=typed_scope, reason=str(exc)))
                continue
            if typed_scope == "workspace" and name in parsed_by_name:
                overridden.append(name)
            parsed_by_name[name] = spec

    # 同源去重：工作区级优先于全局级；同级保留后者还是前者？优先 workspace，同级保留先声明项
    ordered = sorted(
        parsed_by_name.values(),
        key=lambda item: (0 if item.scope == "workspace" else 1, item.name),
    )
    seen_fingerprints: dict[str, str] = {}
    final_specs: list[McpServerSpec] = []
    deduplicated: list[str] = []
    for spec in ordered:
        existing_name = seen_fingerprints.get(spec.endpoint_fingerprint)
        if existing_name is not None:
            deduplicated.append(spec.name)
            issues.append(
                McpConfigIssue(
                    server_name=spec.name,
                    scope=spec.scope,
                    reason=f"Duplicate endpoint_fingerprint with {existing_name!r}",
                )
            )
            continue
        seen_fingerprints[spec.endpoint_fingerprint] = spec.name
        final_specs.append(spec)

    final_specs.sort(key=lambda item: item.name)
    return McpMergedConfig(
        servers=tuple(final_specs),
        overridden_names=tuple(sorted(overridden)),
        deduplicated_names=tuple(sorted(deduplicated)),
        issues=tuple(issues),
    )


def _read_json_file(path: Path | None) -> Mapping[str, object] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"MCP config {path} must be a JSON object")
    return cast(dict[str, object], loaded)


def load_mcp_configs(
    *,
    global_path: Path | None = None,
    workspace_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> McpMergedConfig:
    """从 `~/.agent/mcp.json` 与 `<work_path>/.agent/mcp.json` 读取并合并配置（不向上继承父目录）。"""
    resolved_global = Path.home() / ".agent" / "mcp.json" if global_path is None else global_path
    resolved_workspace = (workspace_root / ".agent" / "mcp.json") if workspace_root is not None else None
    return merge_mcp_configs(
        global_config=_read_json_file(resolved_global),
        workspace_config=_read_json_file(resolved_workspace),
        environ=environ,
    )


def merge_workspace_mcp_specs(
    base_specs: Sequence[McpServerSpec],
    base_prefixes: Sequence[str],
    work_path: str | Path | None,
) -> tuple[tuple[McpServerSpec, ...], tuple[str, ...]]:
    """按任务工作区动态合并 `<work_path>/.agent/mcp.json` 与启动期全局 MCP 配置。"""
    if not work_path:
        return tuple(base_specs), tuple(base_prefixes)
    by_name: dict[str, McpServerSpec] = {s.name: s for s in base_specs}
    try:
        loaded = load_mcp_configs(workspace_root=Path(work_path).expanduser().resolve())
        for spec in loaded.servers:
            by_name[spec.name] = spec
    except (OSError, ValueError):
        pass
    specs = tuple(by_name.values())
    prefixes: set[str] = set(base_prefixes)
    for spec in specs:
        if isinstance(spec.tool_prefix, str) and spec.tool_prefix:
            prefixes.add(spec.tool_prefix)
        elif spec.tool_prefix is not False:
            prefixes.add(f"mcp_{spec.name}_")
    return specs, tuple(sorted(prefixes))
