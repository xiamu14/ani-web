"""MCP 认证构建与持久化 OAuth Token 存储（外部扩展 OAuth 授权与 Role 运行期静态 Bearer 严格解耦）。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import time
import webbrowser
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast, override

import httpx2
import logfire
from cryptography.fernet import Fernet, InvalidToken
from fastmcp.client.auth import BearerAuth, ClientCredentialsOAuthProvider, OAuth
from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.filetree.store import (
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from agent.extensions.mcp_config import McpServerSpec

DEFAULT_MCP_TOKEN_ROOT = Path.home() / ".agent" / "mcp_tokens"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_slug(raw: str) -> str:
    cleaned = _SLUG_RE.sub("_", raw.strip()).strip("._-")
    return cleaned or "server"


def token_namespace_dir(spec: McpServerSpec, *, token_root: Path | None = None) -> Path:
    """按 `(scope, server_name, endpoint_fingerprint)` 隔离本地 OAuth Token 目录。"""
    root = DEFAULT_MCP_TOKEN_ROOT if token_root is None else token_root
    slug = _safe_slug(spec.name)
    return root / f"{spec.scope}__{slug}__{spec.endpoint_fingerprint[:16]}"


def build_token_storage(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
) -> AsyncKeyValue:
    """为指定 MCP Server 创建独立目录权限为 `0700` 的持久化 `AsyncKeyValue` 存储。"""
    ns_dir = token_namespace_dir(spec, token_root=token_root)
    ns_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ns_dir, 0o700)
    store: AsyncKeyValue = FileTreeStore(
        data_directory=ns_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory=ns_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory=ns_dir),
    )
    if encryption_key:
        key_bytes = encryption_key.encode("utf-8") if isinstance(encryption_key, str) else encryption_key
        store = FernetEncryptionWrapper(key_value=store, fernet=Fernet(key_bytes))
    return store


def _decode_store_value(
    raw_file_obj: object, *, encryption_key: str | bytes | None = None
) -> dict[str, object] | None:
    if not isinstance(raw_file_obj, dict):
        return None
    d = cast(dict[str, object], raw_file_obj)
    val = d.get("value")
    target = cast(dict[str, object], val) if isinstance(val, dict) else d
    enc_b64 = target.get("__encrypted_data__")
    if isinstance(enc_b64, str):
        if not encryption_key:
            return None
        key_bytes = (
            encryption_key.encode("utf-8")
            if isinstance(encryption_key, str)
            else encryption_key
        )
        try:
            decrypted = Fernet(key_bytes).decrypt(base64.b64decode(enc_b64))
            parsed = cast(object, json.loads(decrypted.decode("utf-8")))
            return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None
        except (InvalidToken, ValueError, OSError):
            return None
    return target


def _extract_expires_at(
    ns_dir: Path, *, encryption_key: str | bytes | None = None
) -> float | None:
    for p in ns_dir.rglob("*.json"):
        if p.name.endswith("-info.json"):
            continue
        if "token_expiry" not in p.name and "token_expiry" not in p.parent.name:
            continue
        try:
            raw = cast(object, json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        target = _decode_store_value(raw, encryption_key=encryption_key)
        if target is not None:
            exp = target.get("expires_at")
            if isinstance(exp, (int, float)):
                return float(exp)
    return None


def _is_oauth_token_entry_file(path: Path) -> bool:
    """区分真正的持久化 Token 条目与 FileTreeStore 集合元数据(-info.json)及动态注册 client_info。"""
    if not path.is_file():
        return False
    name = path.name
    parent_name = path.parent.name
    if name.endswith("-info.json"):
        return False
    if "client_info" in name or "client_info" in parent_name:
        return False
    return not ("token_expiry" in name or "token_expiry" in parent_name)


def extract_persisted_access_token(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
) -> str | None:
    """无阻塞读取本地已落盘且未过期的 OAuth `access_token`（供 Role Agent 静态 BearerAuth 使用）。"""
    ns_dir = token_namespace_dir(spec, token_root=token_root)
    if not ns_dir.exists() or not ns_dir.is_dir():
        return None
    expires_at = _extract_expires_at(ns_dir, encryption_key=encryption_key)
    if expires_at is not None and expires_at <= time.time() + 30.0:
        return None
    for path in ns_dir.rglob("*.json"):
        if not _is_oauth_token_entry_file(path):
            continue
        try:
            raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        target = _decode_store_value(raw, encryption_key=encryption_key)
        if target is None:
            if isinstance(raw, dict):
                val = cast(dict[str, object], raw).get("value")
                if isinstance(val, dict) and "__encrypted_data__" in val:
                    return "__encrypted_token_present__"
            continue
        token = target.get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def has_persisted_oauth_files(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
) -> bool:
    """无阻塞检查本地是否已落盘该 Server 的真实有效 OAuth `access_token`（排除 client_info 与空集合元数据）。"""
    return (
        extract_persisted_access_token(
            spec, token_root=token_root, encryption_key=encryption_key
        )
        is not None
    )


def _is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def resolve_oauth_callback_port(
    spec: McpServerSpec, *, token_root: Path | None = None
) -> int:
    """为 OAuth 回调解析稳定端口，避免随机端口导致旧 client_id 的 redirect_uri 失配。"""
    if spec.auth.callback_port is not None:
        return spec.auth.callback_port
    ns_dir = token_namespace_dir(spec, token_root=token_root)
    if ns_dir.is_dir():
        for p in ns_dir.rglob("*.json"):
            if "client_info" not in p.name or p.name.endswith("-info.json"):
                continue
            try:
                raw = cast(object, json.loads(p.read_text(encoding="utf-8")))
                target = _decode_store_value(raw)
                if target is not None:
                    uris = target.get("redirect_uris")
                    if isinstance(uris, list) and uris:
                        m = re.search(r":(\d+)/", str(uris[0]))
                        if m:
                            port = int(m.group(1))
                            if _is_port_available(port):
                                return port
            except (OSError, ValueError):
                pass
    seed = int((spec.endpoint_fingerprint or spec.name)[:8], 16) if spec.endpoint_fingerprint else 0
    candidate = 49152 + (seed % 10000)
    for offset in range(20):
        if _is_port_available(candidate + offset):
            return candidate + offset
    return 0


def clear_oauth_cache(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    clear_client_info: bool = True,
) -> None:
    """清理过期 Token 及失效的动态注册 client_info，避免旧 redirect_uri 死循环。"""
    ns_dir = token_namespace_dir(spec, token_root=token_root)
    if not ns_dir.is_dir():
        return
    for p in list(ns_dir.rglob("*.json")):
        if not clear_client_info and "client_info" in p.name:
            continue
        try:
            p.unlink()
        except OSError:
            pass


class LocalOAuthReadinessChecker:
    """检查 OAuth Server 是否已具备有效落盘 Access Token（绝不在后台触发任何 OAuth 流程）。"""

    def __init__(
        self,
        *,
        token_root: Path | None = None,
        encryption_key: str | bytes | None = None,
    ) -> None:
        self._token_root: Path | None = token_root
        self._encryption_key: str | bytes | None = encryption_key

    async def is_ready(self, spec: McpServerSpec) -> bool:
        if spec.auth.type != "oauth":
            return True
        return has_persisted_oauth_files(
            spec,
            token_root=self._token_root,
            encryption_key=self._encryption_key,
        )


class _InteractiveOAuth(OAuth):
    """仅限外部扩展授权接口（POST /v1/mcps/{name}/auth 与 CLI mcp auth）使用的交互式 OAuth 驱动。"""

    @override
    async def redirect_handler(self, authorization_url: str) -> None:
        """直接拉起系统浏览器完成授权（跳过 fastmcp 默认使用后端 httpx2 发起的易失败预检 GET）。"""
        logfire.info(
            "Opening system browser for MCP OAuth authorization",
            mcp_url=self.mcp_url,
            authorization_url=authorization_url,
        )
        _ = webbrowser.open(authorization_url)

    @override
    async def _auth_flow(
        self, request: Any
    ) -> AsyncGenerator[Any, Any]:
        flow = super()._auth_flow(request)
        try:
            first_req = await anext(flow)
            resp = yield first_req
            if not self.context.is_token_valid() and getattr(resp, "status_code", 200) < 400:
                resp.status_code = 401
            next_req = await flow.asend(resp)
            while True:
                next_resp = yield next_req
                next_req = await flow.asend(next_resp)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()


async def run_interactive_oauth(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
) -> None:
    """独立于 MCPToolset 执行外部扩展 OAuth 交互授权并持久化 Token（支持瞬时网络重连）。"""
    if spec.auth.type != "oauth" or not spec.url:
        raise ValueError(f"MCP server {spec.name!r} is not an HTTP/SSE OAuth server.")
    clear_oauth_cache(spec, token_root=token_root, clear_client_info=False)
    oauth = build_fastmcp_auth(
        spec, token_root=token_root, encryption_key=encryption_key, interactive=True
    )
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx2.AsyncClient(
                auth=oauth,
                verify=spec.verify,
                timeout=30.0,
                follow_redirects=False,
            ) as client:
                _ = await client.post(
                    spec.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "super-agent-oauth", "version": "1.0.0"},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
            return
        except httpx2.ConnectError as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            raise
    if last_exc is not None:
        raise last_exc


def build_fastmcp_auth(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
    interactive: bool = False,
) -> Any:
    """构建 MCP 认证提供者：运行期(interactive=False)严格只用静态 BearerAuth，仅外部扩展授权(interactive=True)挂载 _InteractiveOAuth。"""
    auth = spec.auth
    if auth.type == "none":
        return None
    if auth.type == "bearer":
        if not auth.token:
            raise ValueError(f"MCP server {spec.name!r} uses bearer auth without `token`")
        return BearerAuth(auth.token)
    if auth.type == "oauth":
        if not interactive:
            access_token = extract_persisted_access_token(
                spec, token_root=token_root, encryption_key=encryption_key
            )
            return BearerAuth(access_token) if access_token else None
        storage = build_token_storage(spec, token_root=token_root, encryption_key=encryption_key)
        scopes = list(auth.scopes) if auth.scopes else None
        cb_port = resolve_oauth_callback_port(spec, token_root=token_root)
        return _InteractiveOAuth(
            mcp_url=spec.url,
            scopes=scopes,
            token_storage=storage,
            callback_port=cb_port or None,
            callback_timeout=180.0,
            client_metadata_url=auth.client_metadata_url,
            client_id=auth.client_id,
            client_secret=auth.client_secret,
        )
    if auth.type == "oauth_client_credentials":
        if not auth.client_id or not auth.client_secret:
            raise ValueError(
                f"MCP server {spec.name!r} uses oauth_client_credentials without client_id/client_secret"
            )
        storage = build_token_storage(spec, token_root=token_root, encryption_key=encryption_key)
        scopes = list(auth.scopes) if auth.scopes else None
        return ClientCredentialsOAuthProvider(
            mcp_url=spec.url,
            client_id=auth.client_id,
            client_secret=auth.client_secret,
            scopes=scopes,
            token_endpoint_auth_method=auth.token_endpoint_auth_method,
            issuer=auth.issuer,
            token_storage=storage,
        )
    raise ValueError(
        f"MCP auth type {auth.type!r} for {spec.name!r} requires a custom assertion provider"
    )
