"""Layer 2 人机交互中心路由（逐条审批 / AskUser / MCP Elicitation）。"""

from __future__ import annotations

from pydantic import TypeAdapter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.protocol import InteractionRespondRequest
from agent.server.state import get_state, json_error, json_model, path_param

_RESPOND_ADAPTER: TypeAdapter[InteractionRespondRequest] = TypeAdapter(
    InteractionRespondRequest
)


async def list_interactions_endpoint(request: Request) -> Response:
    broker = get_state(request).runtime.interaction_broker
    if broker is None:
        return JSONResponse({"interactions": []})
    cid = request.query_params.get("conversation_id")
    tid = request.query_params.get("task_id")
    items = broker.list_pending(conversation_id=cid, task_id=tid)
    return JSONResponse(
        {"interactions": [item.model_dump(mode="json") for item in items]}
    )


async def respond_interaction_endpoint(request: Request) -> Response:
    broker = get_state(request).runtime.interaction_broker
    if broker is None:
        return json_error(
            request,
            status_code=503,
            code="INTERACTION_BROKER_DISABLED",
            message="Interaction broker is not enabled on this runtime.",
        )
    iid = path_param(request, "interaction_id")
    payload = _RESPOND_ADAPTER.validate_json(await request.body())
    resolved = await broker.respond(iid, payload)
    return json_model(resolved)


def get_interaction_routes() -> list[Route]:
    return [
        Route("/v1/interactions", list_interactions_endpoint, methods=["GET"]),
        Route(
            "/v1/interactions/{interaction_id}/respond",
            respond_interaction_endpoint,
            methods=["POST"],
        ),
    ]
