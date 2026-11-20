"""Layer 2 传输安全鉴权：UDS trusted_local 判定、Bearer Token、Host 与 Origin 校验。"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Sequence
from typing import cast, override
from urllib.parse import urlparse
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from agent.protocol import ErrorEnvelope

_ALLOWED_LOCAL_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}
)


def is_trusted_local_request(request: Request) -> bool:
    """判定请求是否来自本机 Unix Domain Socket（0600 权限）。"""
    if request.scope.get("transport_kind") == "uds":
        return True
    client: object = request.scope.get("client")
    if client is None:
        return True
    if isinstance(client, (tuple, list)):
        seq = cast(Sequence[object], client)
        if len(seq) >= 1:
            host = str(seq[0])
            if host == "" or host.startswith("/"):
                return True
    return False


def _is_allowed_host(raw_host: str | None) -> bool:
    if not raw_host:
        return True
    host_only = raw_host.strip().lower()
    if host_only.startswith("["):
        end_idx = host_only.find("]")
        host_only = host_only[: end_idx + 1] if end_idx != -1 else host_only
    elif ":" in host_only:
        host_only = host_only.rsplit(":", 1)[0]
    return host_only in _ALLOWED_LOCAL_HOSTS


def _is_allowed_origin(origin: str, extra_origins: tuple[str, ...]) -> bool:
    if origin in extra_origins:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname in ("localhost", "127.0.0.1", "::1")


def _error_response(
    status_code: int, *, code: str, message: str, request_id: str
) -> JSONResponse:
    env = ErrorEnvelope(
        code=code, message=message, retryable=False, request_id=request_id
    )
    return JSONResponse(status_code=status_code, content=env.model_dump(mode="json"))


class ProtocolAuthMiddleware(BaseHTTPMiddleware):
    """对 Localhost HTTP 强制执行 Bearer Token、Host 与 Origin 白名单校验；UDS 直通。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth_token: str,
        allowed_origins: Sequence[str] = (),
    ) -> None:
        super().__init__(app)
        self._auth_token: str = auth_token
        self._allowed_origins: tuple[str, ...] = tuple(allowed_origins)

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        req_id = uuid4().hex[:16]
        trusted_local = is_trusted_local_request(request)
        request.state.request_id = req_id
        request.state.trusted_local = trusted_local

        if request.url.path == "/v1/health":
            return await call_next(request)

        if not trusted_local:
            if not _is_allowed_host(request.headers.get("host")):
                return _error_response(
                    403,
                    code="FORBIDDEN_HOST",
                    message="Host header not allowed.",
                    request_id=req_id,
                )
            origin = request.headers.get("origin")
            if origin is not None and not _is_allowed_origin(origin, self._allowed_origins):
                return _error_response(
                    403,
                    code="FORBIDDEN_ORIGIN",
                    message="Origin header not allowed.",
                    request_id=req_id,
                )
            auth_hdr = request.headers.get("authorization", "")
            prefix = "Bearer "
            provided = auth_hdr[len(prefix) :] if auth_hdr.startswith(prefix) else ""
            if not provided or not hmac.compare_digest(provided, self._auth_token):
                return _error_response(
                    401,
                    code="UNAUTHORIZED",
                    message="Missing or invalid Bearer token.",
                    request_id=req_id,
                )

        return await call_next(request)
