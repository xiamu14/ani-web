"""Layer 2 附件上传、UDS 本地零拷贝注册、HTTP Range 预览与 Artifact 路由。"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent.attachments.ports import AttachmentRef
from agent.protocol import (
    ArtifactView,
    AttachmentRegisteredPayload,
    AttachmentView,
    LocalAttachmentRequest,
)
from agent.server.state import (
    get_state,
    is_trusted_local,
    json_error,
    json_model,
    path_param,
)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _to_attachment_view(ref: AttachmentRef) -> AttachmentView:
    return AttachmentView(
        attachment_id=ref.attachment_id,
        sha256=ref.sha256,
        title=ref.title,
        media_type=ref.media_type,
        kind=ref.kind,
        byte_size=ref.byte_size,
        page_count=ref.page_count,
        duration_sec=ref.duration_sec,
        width=ref.width,
        height=ref.height,
        summary=ref.summary,
        reference_handle=ref.to_reference_handle(),
        content_url=f"/v1/attachments/{ref.attachment_id}/content",
    )


async def upload_attachment_endpoint(request: Request) -> Response:
    rt = get_state(request).runtime
    form = await request.form()
    uploaded: list[AttachmentView] = []
    for _field, value in form.multi_items():
        if isinstance(value, UploadFile):
            try:
                data = await value.read()
            finally:
                await value.close()
            filename = value.filename or "upload.bin"
            mime = value.content_type
            if not mime or mime == "application/octet-stream":
                guessed, _ = mimetypes.guess_type(filename)
                mime = guessed or "application/octet-stream"
            ref = rt.attachment_registry.register_bytes(
                data, title=filename, media_type=mime
            )
            view = _to_attachment_view(ref)
            uploaded.append(view)
            if rt.event_bus is not None:
                _ = rt.event_bus.publish(
                    "attachment.registered",
                    AttachmentRegisteredPayload(attachment=view),
                )
    if not uploaded:
        return json_error(
            request,
            status_code=400,
            code="INVALID_ARGUMENT",
            message="No multipart file found in request.",
        )
    return JSONResponse(
        status_code=201,
        content={
            "attachment": uploaded[0].model_dump(mode="json"),
            "attachments": [u.model_dump(mode="json") for u in uploaded],
        },
    )


async def register_local_attachment_endpoint(request: Request) -> Response:
    if not is_trusted_local(request):
        return json_error(
            request,
            status_code=403,
            code="UNTRUSTED_LOCAL_ENDPOINT",
            message="/v1/attachments/local is only allowed over trusted local UDS connections.",
        )
    body = LocalAttachmentRequest.model_validate_json(await request.body())
    target = Path(body.file_path).expanduser().resolve()
    if not target.is_file():
        return json_error(
            request,
            status_code=404,
            code="NOT_FOUND",
            message=f"File not found: {target}",
        )
    rt = get_state(request).runtime
    ref = rt.attachment_registry.register_file(
        target,
        title=body.title,
        media_type=body.media_type,
        duration_sec=body.duration_sec,
        width=body.width,
        height=body.height,
        summary=body.summary,
    )
    view = _to_attachment_view(ref)
    if rt.event_bus is not None:
        _ = rt.event_bus.publish(
            "attachment.registered",
            AttachmentRegisteredPayload(attachment=view),
        )
    return json_model(view, status_code=201)


async def get_attachment_endpoint(request: Request) -> Response:
    att_id = path_param(request, "att_id")
    ref = get_state(request).runtime.attachment_registry.get_attachment(att_id)
    if ref is None:
        return json_error(
            request, status_code=404, code="NOT_FOUND", message=f"Attachment {att_id!r} not found."
        )
    return json_model(_to_attachment_view(ref))


async def get_attachment_content_endpoint(request: Request) -> Response:
    att_id = path_param(request, "att_id")
    reg = get_state(request).runtime.attachment_registry
    ref = reg.get_attachment(att_id)
    if ref is None:
        return json_error(
            request, status_code=404, code="NOT_FOUND", message=f"Attachment {att_id!r} not found."
        )
    raw = reg.get_attachment_bytes(ref.attachment_id)
    if raw is None:
        return json_error(
            request, status_code=404, code="NOT_FOUND", message=f"Content missing for {att_id!r}."
        )
    total = len(raw)
    etag = f'"{ref.sha256}"'
    base_headers = {"Accept-Ranges": "bytes", "ETag": etag}
    range_hdr = request.headers.get("range")
    if range_hdr:
        match = _RANGE_RE.match(range_hdr.strip())
        if match is None:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        start_s, end_s = match.groups()
        if not start_s and not end_s:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        else:
            suffix_len = int(end_s)
            start = max(0, total - suffix_len)
            end = total - 1
        if start >= total or end < start:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        end = min(end, total - 1)
        chunk = raw[start : end + 1]
        return Response(
            content=chunk,
            status_code=206,
            media_type=ref.media_type,
            headers={**base_headers, "Content-Range": f"bytes {start}-{end}/{total}"},
        )
    return Response(content=raw, status_code=200, media_type=ref.media_type, headers=base_headers)


async def list_artifacts_endpoint(request: Request) -> Response:
    scope = request.query_params.get("scope", "global")
    arts = get_state(request).runtime.attachment_registry.list_artifacts(scope=scope)
    views = [
        ArtifactView(
            artifact_id=a.artifact_id,
            scope=a.scope,
            byte_size=a.byte_size,
            sha256=a.sha256,
            description=a.description,
            reference_handle=a.to_reference_handle(),
        ).model_dump(mode="json")
        for a in arts.values()
    ]
    return JSONResponse({"artifacts": views})


async def get_artifact_endpoint(request: Request) -> Response:
    art_id = path_param(request, "art_id")
    scope = request.query_params.get("scope", "global")
    art = get_state(request).runtime.attachment_registry.get_artifact(art_id, scope=scope)
    if art is None:
        return json_error(
            request, status_code=404, code="NOT_FOUND", message=f"Artifact {art_id!r} not found."
        )
    return json_model(
        ArtifactView(
            artifact_id=art.artifact_id,
            scope=art.scope,
            byte_size=art.byte_size,
            sha256=art.sha256,
            description=art.description,
            reference_handle=art.to_reference_handle(),
            value=art.value,  # pyright: ignore[reportArgumentType]
        )
    )


def get_media_routes() -> list[Route]:
    return [
        Route("/v1/attachments", upload_attachment_endpoint, methods=["POST"]),
        Route("/v1/attachments/local", register_local_attachment_endpoint, methods=["POST"]),
        Route("/v1/attachments/{att_id}", get_attachment_endpoint, methods=["GET"]),
        Route("/v1/attachments/{att_id}/content", get_attachment_content_endpoint, methods=["GET"]),
        Route("/v1/artifacts", list_artifacts_endpoint, methods=["GET"]),
        Route("/v1/artifacts/{art_id}", get_artifact_endpoint, methods=["GET"]),
    ]
