"""MCP 认证构建与持久化 OAuth Token 存储（复用 fastmcp.client.auth + py-key-value-aio）。"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, cast, override

from cryptography.fernet import Fernet
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


def _extract_expires_at(ns_dir: Path) -> float | None:
    for p in ns_dir.rglob("*.json"):
        if "token_expiry" not in p.name:
            continue
        try:
            raw = cast(object, json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            d = cast(dict[str, object], raw)
            val = d.get("value")
            target = cast(dict[str, object], val) if isinstance(val, dict) else d
            exp = target.get("expires_at")
            if isinstance(exp, (int, float)):
                return float(exp)
    return None


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
                if isinstance(raw, dict):
                    d = cast(dict[str, object], raw)
                    v = d.get("value")
                    info = cast(dict[str, object], v) if isinstance(v, dict) else d
                    uris = info.get("redirect_uris")
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


def has_persisted_oauth_files(spec: McpServerSpec, *, token_root: Path | None = None) -> bool:
    """无阻塞检查本地是否已落盘该 Server 的 OAuth 凭据文件。"""
    ns_dir = token_namespace_dir(spec, token_root=token_root)
    if not ns_dir.exists() or not ns_dir.is_dir():
        return False
    return any(path.is_file() for path in ns_dir.rglob("*"))


class LocalOAuthReadinessChecker:
    """检查 OAuth Server 是否可无头连接（含过期校验，绝不在后台阻塞等待浏览器）。"""

    def __init__(self, *, token_root: Path | None = None) -> None:
        self._token_root: Path | None = token_root

    async def is_ready(self, spec: McpServerSpec) -> bool:
        if spec.auth.type != "oauth":
            return True
        if not has_persisted_oauth_files(spec, token_root=self._token_root):
            return False
        ns_dir = token_namespace_dir(spec, token_root=self._token_root)
        expires_at = _extract_expires_at(ns_dir)
        return expires_at is None or expires_at > time.time() + 30.0


class _HeadlessOAuth(OAuth):
    """后台无头模式下禁止静默弹浏览器，未授权/过期直接快速失败。"""

    @override
    async def redirect_handler(self, authorization_url: str) -> None:
        raise RuntimeError(
            f"OAuth authorization required (headless mode): {authorization_url}"
        )


def build_fastmcp_auth(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
    interactive: bool = False,
) -> Any:
    """将 McpAuthSpec 映射为 fastmcp.client.auth 或 BearerAuth 实例。"""
    auth = spec.auth
    if auth.type == "none":
        return None
    if auth.type == "bearer":
        if not auth.token:
            raise ValueError(f"MCP server {spec.name!r} uses bearer auth without `token`")
        return BearerAuth(auth.token)
    if auth.type == "oauth":
        storage = build_token_storage(spec, token_root=token_root, encryption_key=encryption_key)
        scopes = list(auth.scopes) if auth.scopes else None
        cb_port = resolve_oauth_callback_port(spec, token_root=token_root)
        oauth_cls = OAuth if interactive else _HeadlessOAuth
        return oauth_cls(
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
