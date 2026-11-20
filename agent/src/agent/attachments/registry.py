"""跨对话、跨 Role Agent 共享的附件与内部产物注册表（AttachmentRegistry）。

统一复用 CompressedFileMediaStore 读写底层 CAS 字节，彻底消除与 ConversationStore 共用目录时的 zlib 压缩格式冲突；
支持 SHA-256 内容去重、UploadedFile / 全模态 URL（含 AudioUrl）缓存及内部 Artifact ID 快速引用。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import override

from pydantic_ai.messages import (
    AudioUrl,
    BinaryContent,
    DocumentUrl,
    ImageUrl,
    UploadedFile,
    UploadedFileProviderName,
    VideoUrl,
)
from pydantic_ai_harness.media._store import MediaContext

from agent.attachments.citations import resolve_artifact_key_path
from agent.attachments.ports import (
    ArtifactRef,
    AttachmentRef,
    AttachmentRegistryPort,
    infer_attachment_kind,
)
from agent.db.media_store import CompressedFileMediaStore


def _guess_mime(path: Path, default: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return "text/markdown"
    if suffix in (".py", ".ts", ".js", ".json", ".toml", ".yaml", ".yml", ".txt", ".log"):
        return "text/plain"
    return default


def _split_document_pages(text: str, *, lines_per_page: int = 60) -> tuple[str, ...]:
    """对文本/文档进行分页：若含显式换页符 \\f 则按换页符切分，否则每 lines_per_page 行切为一页。"""
    if "\f" in text:
        pages = [p for p in text.split("\f") if p.strip()]
        return tuple(pages) if pages else (text,)
    lines = text.splitlines()
    if not lines:
        return ("",)
    chunk_size = max(1, lines_per_page)
    return tuple(
        "\n".join(lines[i : i + chunk_size]) for i in range(0, len(lines), chunk_size)
    )


class AttachmentRegistry(AttachmentRegistryPort):
    """Runtime 级跨对话、跨 Role 共享的附件与内部产物注册表（统一复用 CompressedFileMediaStore）。"""

    def __init__(
        self,
        *,
        media_store: CompressedFileMediaStore | None = None,
        storage_dir: str | Path | None = None,
    ) -> None:
        if media_store is not None:
            self._media_store: CompressedFileMediaStore | None = media_store
        elif storage_dir is not None:
            self._media_store = CompressedFileMediaStore(storage_dir)
        else:
            self._media_store = None
        self._attachments: dict[str, AttachmentRef] = {}
        self._bytes_cache: dict[str, bytes] = {}
        self._pages_cache: dict[str, tuple[str, ...]] = {}
        self._artifacts: dict[tuple[str, str], ArtifactRef] = {}

    def register_bytes(
        self,
        data: bytes,
        *,
        title: str,
        media_type: str,
        page_count: int | None = None,
        pages: Sequence[str] | None = None,
        duration_sec: float | None = None,
        width: int | None = None,
        height: int | None = None,
        summary: str = "",
        public_url: str | None = None,
        uploaded_file_id: str | None = None,
        uploaded_provider: UploadedFileProviderName | None = None,
    ) -> AttachmentRef:
        """注册多类型文件附件：基于 SHA-256 跨对话/跨 Role 自动去重，返回全局唯一 AttachmentRef。"""
        digest = hashlib.sha256(data).hexdigest()
        attachment_id = f"att_{digest[:12]}"
        kind = infer_attachment_kind(media_type)

        resolved_pages: tuple[str, ...] | None = None
        if pages is not None:
            resolved_pages = tuple(pages)
        elif kind == "document" and (
            media_type.startswith("text/")
            or media_type in ("application/json", "application/markdown")
        ):
            decoded = data.decode("utf-8", errors="replace")
            resolved_pages = _split_document_pages(decoded)

        effective_page_count = (
            page_count
            if page_count is not None
            else (len(resolved_pages) if resolved_pages is not None else None)
        )

        if self._media_store is not None:
            uri = self._media_store.put_sync(
                data,
                MediaContext(media_type=media_type, filename=title),
            )
        else:
            uri = f"media+sha256://{digest}"
            self._bytes_cache[attachment_id] = data

        if resolved_pages is not None:
            self._pages_cache[attachment_id] = resolved_pages

        existing = self._attachments.get(attachment_id)
        if existing is not None:
            if uploaded_file_id is not None or public_url is not None:
                updated = AttachmentRef(
                    attachment_id=existing.attachment_id,
                    sha256=existing.sha256,
                    uri=existing.uri,
                    title=existing.title,
                    media_type=existing.media_type,
                    kind=existing.kind,
                    byte_size=existing.byte_size,
                    page_count=effective_page_count or existing.page_count,
                    duration_sec=duration_sec if duration_sec is not None else existing.duration_sec,
                    width=width if width is not None else existing.width,
                    height=height if height is not None else existing.height,
                    summary=summary or existing.summary,
                    public_url=public_url or existing.public_url,
                    uploaded_file_id=uploaded_file_id or existing.uploaded_file_id,
                    uploaded_provider=uploaded_provider or existing.uploaded_provider,
                )
                self._attachments[attachment_id] = updated
                return updated
            return existing

        ref = AttachmentRef(
            attachment_id=attachment_id,
            sha256=digest,
            uri=uri,
            title=title,
            media_type=media_type,
            kind=kind,
            byte_size=len(data),
            page_count=effective_page_count,
            duration_sec=duration_sec,
            width=width,
            height=height,
            summary=summary,
            public_url=public_url,
            uploaded_file_id=uploaded_file_id,
            uploaded_provider=uploaded_provider,
        )
        self._attachments[attachment_id] = ref
        return ref

    def register_file(
        self,
        file_path: str | Path,
        *,
        title: str | None = None,
        media_type: str | None = None,
        duration_sec: float | None = None,
        width: int | None = None,
        height: int | None = None,
        summary: str = "",
    ) -> AttachmentRef:
        """从本地文件注册附件（SHA-256 自动去重）。"""
        path = Path(file_path).expanduser().resolve()
        data = path.read_bytes()
        mime = media_type or _guess_mime(path)
        return self.register_bytes(
            data,
            title=title or path.name,
            media_type=mime,
            duration_sec=duration_sec,
            width=width,
            height=height,
            summary=summary,
        )

    def bind_uploaded_file(
        self,
        attachment_id: str,
        *,
        file_id: str,
        provider_name: UploadedFileProviderName,
    ) -> AttachmentRef:
        """为附件绑定上游 Provider File API 缓存 ID（跨对话/跨 Role 传输零重传）。"""
        ref = self.get_attachment(attachment_id)
        if ref is None:
            raise LookupError(f"Attachment {attachment_id!r} not found.")
        updated = AttachmentRef(
            attachment_id=ref.attachment_id,
            sha256=ref.sha256,
            uri=ref.uri,
            title=ref.title,
            media_type=ref.media_type,
            kind=ref.kind,
            byte_size=ref.byte_size,
            page_count=ref.page_count,
            duration_sec=ref.duration_sec,
            width=ref.width,
            height=ref.height,
            summary=ref.summary,
            public_url=ref.public_url,
            uploaded_file_id=file_id,
            uploaded_provider=provider_name,
        )
        self._attachments[ref.attachment_id] = updated
        return updated

    @override
    def get_attachment(self, attachment_id: str) -> AttachmentRef | None:
        norm = attachment_id.strip().removeprefix("attachment:").removeprefix("att:")
        if not norm.startswith("att_") and f"att_{norm}" in self._attachments:
            return self._attachments[f"att_{norm}"]
        return self._attachments.get(norm)

    def get_attachment_bytes(self, attachment_id: str) -> bytes | None:
        ref = self.get_attachment(attachment_id)
        if ref is None:
            return None
        if ref.attachment_id in self._bytes_cache:
            return self._bytes_cache[ref.attachment_id]
        if self._media_store is not None:
            try:
                return self._media_store.get_sync(ref.uri)
            except FileNotFoundError:
                return None
        return None

    @override
    def get_document_pages(self, attachment_id: str) -> tuple[str, ...] | None:
        ref = self.get_attachment(attachment_id)
        if ref is None:
            return None
        return self._pages_cache.get(ref.attachment_id)

    def get_document_page_slice(
        self,
        attachment_id: str,
        *,
        page: int = 1,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> str:
        """按页码与可选行范围读取文档切片（带 1-based 行号标注以支持精确 [[cite:...]] 溯源）。"""
        ref = self.get_attachment(attachment_id)
        if ref is None:
            raise LookupError(f"Attachment {attachment_id!r} not found.")
        pages = self.get_document_pages(ref.attachment_id)
        if pages is None:
            raise ValueError(
                f"Attachment {ref.attachment_id!r} ({ref.media_type}) does not have text pages."
            )
        if page < 1 or page > len(pages):
            raise ValueError(f"Page {page} out of range (1..{len(pages)}).")
        lines = pages[page - 1].splitlines()
        start_idx = max(1, line_start or 1)
        end_idx = min(len(lines), line_end or len(lines))
        numbered = [
            f"L{ln}: {lines[ln - 1]}"
            for ln in range(start_idx, end_idx + 1)
            if 1 <= ln <= len(lines)
        ]
        header = f"[Attachment {ref.attachment_id} | {ref.title} | Page {page}/{len(pages)}]"
        return header + "\n" + "\n".join(numbered)

    def to_pydantic_ai_content(
        self, attachment_id: str
    ) -> UploadedFile | ImageUrl | AudioUrl | VideoUrl | DocumentUrl | BinaryContent:
        """将附件按最优形态转换为 Pydantic AI 原生内容对象（优先 UploadedFile / URL 缓存，其次 BinaryContent）。"""
        ref = self.get_attachment(attachment_id)
        if ref is None:
            raise LookupError(f"Attachment {attachment_id!r} not found.")
        if ref.uploaded_file_id is not None and ref.uploaded_provider is not None:
            return UploadedFile(
                file_id=ref.uploaded_file_id,
                provider_name=ref.uploaded_provider,
                media_type=ref.media_type,
                identifier=ref.attachment_id,
            )
        if ref.public_url is not None:
            if ref.kind == "image":
                return ImageUrl(url=ref.public_url, media_type=ref.media_type, identifier=ref.attachment_id)
            if ref.kind == "audio":
                return AudioUrl(url=ref.public_url, media_type=ref.media_type, identifier=ref.attachment_id)
            if ref.kind == "video":
                return VideoUrl(url=ref.public_url, media_type=ref.media_type, identifier=ref.attachment_id)
            if ref.kind == "document":
                return DocumentUrl(url=ref.public_url, media_type=ref.media_type, identifier=ref.attachment_id)
        raw = self.get_attachment_bytes(ref.attachment_id)
        if raw is None:
            raise FileNotFoundError(f"Binary payload missing for attachment {ref.attachment_id!r}")
        return BinaryContent(data=raw, media_type=ref.media_type, identifier=ref.attachment_id)

    def save_artifact(
        self,
        artifact_id: str,
        value: object,
        *,
        scope: str = "global",
        description: str = "",
    ) -> ArtifactRef:
        """写入跨工作流/跨 Role 的内部资源产物或状态变量，返回轻量 ArtifactRef。"""
        norm_id = (
            artifact_id.strip()
            .removeprefix("artifact:")
            .removeprefix("art:")
            .removeprefix("state:")
        )
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        digest = hashlib.sha256(serialized).hexdigest()
        rec = ArtifactRef(
            artifact_id=norm_id,
            scope=scope,
            value=value,
            byte_size=len(serialized),
            sha256=digest,
            description=description,
        )
        self._artifacts[(scope, norm_id)] = rec
        return rec

    @override
    def get_artifact(self, artifact_id: str, *, scope: str = "global") -> ArtifactRef | None:
        norm = (
            artifact_id.strip()
            .removeprefix("artifact:")
            .removeprefix("art:")
            .removeprefix("state:")
        )
        return self._artifacts.get((scope, norm))

    def query_artifact_field(
        self,
        artifact_id: str,
        *,
        key_path: str | None = None,
        scope: str = "global",
    ) -> object:
        rec = self.get_artifact(artifact_id, scope=scope)
        if rec is None:
            raise LookupError(f"Artifact {artifact_id!r} not found in scope {scope!r}.")
        found, val = resolve_artifact_key_path(rec.value, key_path)
        if not found:
            raise KeyError(f"Key path {key_path!r} not found in artifact {artifact_id!r}.")
        return val

    @override
    def list_attachments(self) -> list[AttachmentRef]:
        return list(self._attachments.values())

    @override
    def list_artifacts(self, *, scope: str = "global") -> Mapping[str, ArtifactRef]:
        return {k: v for (sc, k), v in self._artifacts.items() if sc == scope}
