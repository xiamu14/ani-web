"""Layer 2 服务端共享状态与统一 JSON 响应辅助函数（零循环依赖）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent.core.runtime import SuperAgentRuntime
from agent.protocol import ErrorEnvelope, SendMessageReceipt


@dataclass
class ServerState:
    """挂载在 Starlette app.state 上的运行时协调容器。"""

    runtime: SuperAgentRuntime
    auth_token: str
    allowed_origins: tuple[str, ...] = ()
    background_runs: dict[str, asyncio.Task[object]] = field(default_factory=dict)
    idempotency_cache: dict[str, SendMessageReceipt] = field(default_factory=dict)


def get_state(request: Request) -> ServerState:
    return cast(ServerState, request.app.state.server_state)  # pyright: ignore[reportAny]


def path_param(request: Request, name: str) -> str:
    return str(request.path_params[name])  # pyright: ignore[reportAny]


def request_id_of(request: Request) -> str:
    rid: object = getattr(request.state, "request_id", None)
    return str(rid) if rid else uuid4().hex[:16]


def is_trusted_local(request: Request) -> bool:
    return bool(getattr(request.state, "trusted_local", False))


def json_model(model: BaseModel, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"))


def json_error(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    detail: dict[str, JsonValue] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    env = ErrorEnvelope(
        code=code,
        message=message,
        detail=detail or {},
        retryable=retryable,
        request_id=request_id_of(request),
    )
    return JSONResponse(status_code=status_code, content=env.model_dump(mode="json"))
