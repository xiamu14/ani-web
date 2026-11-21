"""Layer 2 Starlette 应用装配、统一错误处理与 OpenAPI Schema 导出。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import uuid4

import logfire
from pydantic import JsonValue, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent.core.runtime import SuperAgentRuntime
from agent.protocol import (
    InteractionAlreadyResolvedError,
    InteractionNotFoundError,
    InteractionPayloadError,
)
from agent.server.auth import ProtocolAuthMiddleware
from agent.server.routes_chat import get_chat_routes
from agent.server.routes_interactions import get_interaction_routes
from agent.server.routes_media import get_media_routes
from agent.server.routes_tasks import get_task_routes
from agent.server.routes_telemetry import get_telemetry_routes
from agent.server.routes_tree import get_tree_routes
from agent.server.state import ServerState, json_error


async def _handle_not_found(request: Request, exc: Exception) -> Response:
    return json_error(request, status_code=404, code="NOT_FOUND", message=str(exc))


async def _handle_conflict(request: Request, exc: Exception) -> Response:
    return json_error(
        request,
        status_code=409,
        code="INTERACTION_ALREADY_RESOLVED",
        message=str(exc),
    )


async def _handle_validation(request: Request, exc: Exception) -> Response:
    return json_error(
        request, status_code=422, code="VALIDATION_ERROR", message=str(exc)
    )


async def _handle_internal_error(request: Request, exc: Exception) -> Response:
    logfire.error(
        "Unhandled server exception",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
        error=str(exc) or type(exc).__name__,
    )
    return json_error(
        request,
        status_code=500,
        code=type(exc).__name__,
        message=str(exc) or type(exc).__name__,
    )


def create_app(
    runtime: SuperAgentRuntime,
    *,
    auth_token: str = "",
    allowed_origins: Sequence[str] = (),
) -> Starlette:
    """装配完整 `/v1` REST + SSE Starlette 应用实例。"""
    effective_token = auth_token or uuid4().hex + uuid4().hex
    routes: list[Route] = [
        *get_chat_routes(),
        *get_tree_routes(),
        *get_task_routes(),
        *get_interaction_routes(),
        *get_media_routes(),
        *get_telemetry_routes(),
    ]
    app = Starlette(
        debug=False,
        routes=routes,
        middleware=[
            Middleware(
                ProtocolAuthMiddleware,
                auth_token=effective_token,
                allowed_origins=allowed_origins,
            )
        ],
        exception_handlers={
            LookupError: _handle_not_found,
            InteractionNotFoundError: _handle_not_found,
            InteractionAlreadyResolvedError: _handle_conflict,
            InteractionPayloadError: _handle_validation,
            ValidationError: _handle_validation,
            ValueError: _handle_validation,
            Exception: _handle_internal_error,
        },
    )
    app.state.server_state = ServerState(
        runtime=runtime,
        auth_token=effective_token,
        allowed_origins=tuple(allowed_origins),
    )
    return app


def build_openapi_document(app: Starlette) -> dict[str, JsonValue]:
    """导出完整的 OpenAPI 3.1 文档（含所有 REST 路径与 Pydantic 模型组件）。"""
    from pydantic.json_schema import models_json_schema

    from agent.protocol import (
        BranchView,
        ContextWindowSnapshotView,
        ErrorEnvelope,
        HealthView,
        InteractionView,
        McpServerView,
        McpsListView,
        McpToolPolicyView,
        RuntimeSnapshotView,
        RunTurnView,
        SendMessageReceipt,
        SendMessageRequest,
        SkillsListView,
        SkillView,
        SkippedSkillView,
        TaskView,
        TreeUsageSummaryView,
        TreeView,
        TurnPageView,
        TurnStepView,
    )

    _, top_schema = models_json_schema(
        [
            (ErrorEnvelope, "validation"),
            (HealthView, "validation"),
            (RuntimeSnapshotView, "validation"),
            (TreeView, "validation"),
            (BranchView, "validation"),
            (SendMessageRequest, "validation"),
            (SendMessageReceipt, "validation"),
            (TaskView, "validation"),
            (InteractionView, "validation"),
            (ContextWindowSnapshotView, "validation"),
            (TreeUsageSummaryView, "validation"),
            (SkillsListView, "validation"),
            (SkillView, "validation"),
            (SkippedSkillView, "validation"),
            (McpsListView, "validation"),
            (McpServerView, "validation"),
            (McpToolPolicyView, "validation"),
            (TurnPageView, "validation"),
            (RunTurnView, "validation"),
            (TurnStepView, "validation"),
        ],
        ref_template="#/components/schemas/{model}",
    )
    paths: dict[str, JsonValue] = {}
    for route in app.routes:
        if isinstance(route, Route):
            methods = {
                m.lower(): {
                    "operationId": f"{m.lower()}_{route.name}",
                    "responses": {"200": {"description": "OK"}},
                }
                for m in (route.methods or set())
                if m not in ("HEAD", "OPTIONS")
            }
            if methods:
                paths[route.path] = cast(JsonValue, methods)
    return {
        "openapi": "3.1.0",
        "info": {"title": "Super-Agent GUI Protocol", "version": "1.0.0"},
        "paths": paths,
        "components": {"schemas": cast(JsonValue, top_schema.get("$defs", {}))},
    }


def export_openapi_file(app: Starlette, output_path: str | Path) -> Path:
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = build_openapi_document(app)
    _ = target.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
