"""MCP 认证构建与持久化 OAuth Token 存储（复用 fastmcp.client.auth + py-key-value-aio）。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

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


def has_persisted_oauth_files(spec: McpServerSpec, *, token_root: Path | None = None) -> bool:
    """无阻塞检查本地是否已落盘该 Server 的 OAuth 凭据文件。"""
    ns_dir = token_namespace_dir(spec, token_root=token_root)
    if not ns_dir.exists() or not ns_dir.is_dir():
        return False
    return any(path.is_file() for path in ns_dir.rglob("*"))


class LocalOAuthReadinessChecker:
    """检查 OAuth Server 是否可无头连接（实现 McpAuthReadinessPort）。"""

    def __init__(self, *, token_root: Path | None = None) -> None:
        self._token_root: Path | None = token_root

    async def is_ready(self, spec: McpServerSpec) -> bool:
        if spec.auth.type != "oauth":
            return True
        return has_persisted_oauth_files(spec, token_root=self._token_root)


def build_fastmcp_auth(
    spec: McpServerSpec,
    *,
    token_root: Path | None = None,
    encryption_key: str | bytes | None = None,
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
        return OAuth(
            mcp_url=spec.url,
            scopes=scopes,
            token_storage=storage,
            callback_port=auth.callback_port,
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
