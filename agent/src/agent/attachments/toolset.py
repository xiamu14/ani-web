"""附件与内部产物按需检视工具集（build_attachment_toolset）：支持跨 Role 引用传输、工作区沙箱校验与结果溯源核验。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import FunctionToolset

from agent.attachments.citations import extract_and_verify_citations
from agent.attachments.registry import AttachmentRegistry


def build_attachment_toolset(
    registry: AttachmentRegistry,
    *,
    workspace_root: str | Path | None = None,
    artifact_scope: str = "global",
) -> FunctionToolset[object]:
    """构建供 Role Agent 注册/检视附件、读写内部产物（Artifact）与核验溯源引用的工具集。"""
    ws_path = Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
    toolset: FunctionToolset[object] = FunctionToolset()

    @toolset.tool_plain
    def register_attachment(
        file_path: str,
        title: str | None = None,
        media_type: str | None = None,
        duration_sec: float | None = None,
        width: int | None = None,
        height: int | None = None,
        summary: str = "",
    ) -> str:
        """Register a workspace file (image, audio, video, PDF, or text) into AttachmentRegistry and return a compact [[attachment:...]] handle."""
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute() and ws_path is not None:
            target = (ws_path / candidate).resolve()
        else:
            target = candidate.resolve()
        if ws_path is not None and not target.is_relative_to(ws_path):
            return f"Error: access denied — {file_path!r} escapes bound workspace root."
        if not target.is_file():
            return f"Error: file {file_path!r} does not exist."
        ref = registry.register_file(
            target,
            title=title or candidate.name,
            media_type=media_type,
            duration_sec=duration_sec,
            width=width,
            height=height,
            summary=summary,
        )
        return (
            f"Registered {ref.to_reference_handle()}\n"
            + f"URI: {ref.uri} | SHA256: {ref.sha256[:16]}"
        )

    @toolset.tool_plain
    def inspect_attachment(
        attachment_id: str,
        page: int | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        load_binary: bool = False,
    ) -> str | ToolReturn:
        """Inspect a registered attachment by attachment_id. For documents, pass `page` (and optional `line_start`/`line_end`). Set `load_binary=True` to attach raw image/audio/video content."""
        ref = registry.get_attachment(attachment_id)
        if ref is None:
            return f"Error: Attachment {attachment_id!r} not found in AttachmentRegistry."

        pages = registry.get_document_pages(ref.attachment_id)
        if pages is not None and not load_binary:
            target_page = page if page is not None else 1
            try:
                slice_text = registry.get_document_page_slice(
                    ref.attachment_id,
                    page=target_page,
                    line_start=line_start,
                    line_end=line_end,
                )
            except (LookupError, ValueError) as exc:
                return f"Error: {exc}"
            return (
                f"{slice_text}\n\n"
                + f"Tip: cite facts from this page using `[[cite:{ref.attachment_id}#page={target_page}&line=<start>-<end> | \"exact quote\"]]`."
            )

        if load_binary or ref.kind in ("image", "audio", "video"):
            try:
                content_obj = registry.to_pydantic_ai_content(ref.attachment_id)
            except (LookupError, FileNotFoundError) as exc:
                return f"Error: {exc}"
            return ToolReturn(
                return_value=(
                    f"Loaded attachment {ref.to_reference_handle()}. "
                    + f"Cite regions/timestamps via `[[cite:{ref.attachment_id}#bbox=x1,y1,x2,y2 | \"...\"]]` "
                    + f"or `[[cite:{ref.attachment_id}#t=MM:SS-MM:SS | \"...\"]]`."
                ),
                content=[content_obj],
            )

        return (
            f"Attachment Metadata: {ref.to_reference_handle()}\n"
            + f"Summary: {ref.summary or '(none)'}\n"
            + "Pass `page=1` for text documents or `load_binary=True` to load binary content."
        )

    @toolset.tool_plain
    def list_attachments() -> str:
        """List all globally registered file attachments and internal artifacts."""
        atts = registry.list_attachments()
        arts = registry.list_artifacts(scope=artifact_scope)
        lines: list[str] = ["=== Global File Attachments ==="]
        if not atts:
            lines.append("(no attachments registered)")
        for a in atts:
            lines.append(f"- {a.to_reference_handle()}")
        lines.append("\n=== Internal Artifacts ===")
        if not arts:
            lines.append("(no artifacts registered)")
        for art in arts.values():
            lines.append(f"- {art.to_reference_handle()} {art.description}")
        return "\n".join(lines)

    @toolset.tool_plain
    def save_artifact(
        artifact_id: str,
        json_value: str,
        description: str = "",
    ) -> str:
        """Store or update an internal JSON artifact / state variable accessible across workflows and Role Agents by ID."""
        try:
            parsed: object = json.loads(json_value)  # pyright: ignore[reportAny]
        except json.JSONDecodeError:
            parsed = json_value
        rec = registry.save_artifact(
            artifact_id,
            parsed,
            scope=artifact_scope,
            description=description,
        )
        return (
            f"Stored {rec.to_reference_handle()}. "
            + f"Cite fields via `[[cite:art:{rec.artifact_id}#key=<field.path> | \"value\"]]`."
        )

    @toolset.tool_plain
    def get_artifact(
        artifact_id: str,
        key_path: str | None = None,
    ) -> str:
        """Read an internal artifact by ID (or a nested dot-separated `key_path` such as 'pricing.min_charge')."""
        try:
            val = registry.query_artifact_field(
                artifact_id, key_path=key_path, scope=artifact_scope
            )
        except (LookupError, KeyError) as exc:
            return f"Error: {exc}"
        path_suffix = f"#key={key_path}" if key_path else ""
        rendered = json.dumps(val, ensure_ascii=False)
        return (
            f"[Artifact {artifact_id}{path_suffix}] = {rendered}\n"
            + f"Tip: cite via `[[cite:art:{artifact_id}{path_suffix} | {rendered}]]`."
        )

    @toolset.tool_plain
    def verify_citations(draft_text: str) -> str:
        """Verify all [[cite:...]] provenance tags in a draft answer against AttachmentRegistry physical boundaries."""
        verdicts = extract_and_verify_citations(
            draft_text, registry, artifact_scope=artifact_scope
        )
        if not verdicts:
            return "No [[cite:...]] tags found in draft_text."
        rows: list[str] = []
        for v in verdicts:
            status = "VERIFIED" if v.verified else f"INVALID ({v.verification_reason})"
            rows.append(f"- {v.raw_tag} -> {status}")
        return "\n".join(rows)

    return toolset
