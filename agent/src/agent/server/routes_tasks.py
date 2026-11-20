"""Layer 2 Role Tasks 与工作区读写锁路由。"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.protocol import TaskCancelRequest, TaskSteerRequest
from agent.server.state import get_state, json_error, json_model, path_param


async def list_tasks_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    rt = get_state(request).runtime
    rows = await rt.ticket_store.list_tickets(cid)
    bridge = rt.scheduler.protocol_bridge
    views = [bridge.to_task_view(r).model_dump(mode="json") for r in rows]
    return JSONResponse({"tasks": views})


async def get_task_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    tid = path_param(request, "tid").strip().lstrip("@")
    rt = get_state(request).runtime
    row = await rt.ticket_store.get_ticket(cid, tid)
    if row is None:
        return json_error(request, status_code=404, code="NOT_FOUND", message=f"Task @{tid} not found.")
    return json_model(rt.scheduler.protocol_bridge.to_task_view(row))


async def steer_task_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    tid = path_param(request, "tid")
    body = TaskSteerRequest.model_validate_json(await request.body())
    rt = get_state(request).runtime
    msg = await rt.scheduler.steer_or_revise(
        main_conversation_id=cid, task_id=tid, instruction=body.instruction
    )
    row = await rt.ticket_store.get_ticket(cid, tid.strip().lstrip("@"))
    return JSONResponse(
        {
            "message": msg,
            "task": rt.scheduler.protocol_bridge.to_task_view(row).model_dump(mode="json")
            if row is not None
            else None,
        }
    )


async def cancel_task_endpoint(request: Request) -> Response:
    cid = path_param(request, "cid")
    tid = path_param(request, "tid")
    raw_body = await request.body()
    if raw_body.strip():
        _ = TaskCancelRequest.model_validate_json(raw_body)
    rt = get_state(request).runtime
    msg = await rt.scheduler.cancel_task(main_conversation_id=cid, task_id=tid)
    row = await rt.ticket_store.get_ticket(cid, tid.strip().lstrip("@"))
    return JSONResponse(
        {
            "message": msg,
            "task": rt.scheduler.protocol_bridge.to_task_view(row).model_dump(mode="json")
            if row is not None
            else None,
        }
    )


async def workspace_locks_endpoint(request: Request) -> Response:
    work_path = request.query_params.get("work_path")
    rt = get_state(request).runtime
    snaps = await rt.scheduler.inspect_workspace_locks(work_path)
    locks = [
        {
            "work_path": s.work_path,
            "exclusive_holder": s.exclusive_holder,
            "shared_holders": list(s.shared_holders),
            "queued_tasks": list(s.queued_tasks),
        }
        for s in snaps
    ]
    return JSONResponse({"locks": locks})


def get_task_routes() -> list[Route]:
    return [
        Route("/v1/conversations/{cid}/tasks", list_tasks_endpoint, methods=["GET"]),
        Route("/v1/conversations/{cid}/tasks/{tid}", get_task_endpoint, methods=["GET"]),
        Route("/v1/conversations/{cid}/tasks/{tid}/steer", steer_task_endpoint, methods=["POST"]),
        Route("/v1/conversations/{cid}/tasks/{tid}/cancel", cancel_task_endpoint, methods=["POST"]),
        Route("/v1/workspace-locks", workspace_locks_endpoint, methods=["GET"]),
    ]
