"""多类型文件附件（Attachment）、内部产物（Artifact）与生成结果溯源（Citations）契约模型（Layer 0 零 IO）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic_ai.messages import (
    ContentCitationAnchor,
    DocumentCitationSource,
    UploadedFileProviderName,
)

AttachmentKind = Literal["image", "audio", "video", "document", "binary"]
CitationTargetKind = Literal["attachment", "artifact"]


def infer_attachment_kind(media_type: str) -> AttachmentKind:
    """根据 MIME media_type 推导附件类型。"""
    lowered = media_type.lower().strip()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("audio/"):
        return "audio"
    if lowered.startswith("video/"):
        return "video"
    if (
        lowered == "application/pdf"
        or lowered.startswith("text/")
        or lowered in ("application/json", "application/markdown", "application/xml")
    ):
        return "document"
    return "binary"


@dataclass(frozen=True, kw_only=True)
class AttachmentRef:
    """跨对话、跨 Role Agent 共享的多类型文件附件引用描述符（不可变轻量元数据）。"""

    attachment_id: str
    sha256: str
    uri: str
    title: str
    media_type: str
    kind: AttachmentKind
    byte_size: int
    page_count: int | None = None
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    summary: str = ""
    public_url: str | None = None
    uploaded_file_id: str | None = None
    uploaded_provider: UploadedFileProviderName | None = None

    def to_reference_handle(self) -> str:
        """渲染紧凑的 ~35 tokens 全局附件句柄（供跨对话/跨 Role 传递，保护 KV Cache）。"""
        extras: list[str] = [self.media_type, f"{self.byte_size}B"]
        if self.page_count is not None:
            extras.append(f"pages={self.page_count}")
        if self.duration_sec is not None:
            extras.append(f"duration={self.duration_sec:.1f}s")
        if self.width is not None and self.height is not None:
            extras.append(f"{self.width}x{self.height}")
        meta = ", ".join(extras)
        return f"[[attachment:{self.attachment_id} | {self.title} ({meta})]]"


@dataclass(frozen=True, kw_only=True)
class ArtifactRef:
    """工作流与 Multi-Agent 内部资源产物 / 状态变量快速引用描述符。"""

    artifact_id: str
    scope: str = "global"
    value: object
    byte_size: int
    sha256: str
    description: str = ""

    def to_reference_handle(self) -> str:
        return f"[[artifact:{self.artifact_id} | json ({self.byte_size}B)]]"


@dataclass(frozen=True, kw_only=True)
class CitationLocator:
    """生成结果溯源物理定位器（支持文档页码/行区间、音视频时间戳、图像 BBox、内部产物键路径）。"""

    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    time_start_sec: float | None = None
    time_end_sec: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    artifact_key_path: str | None = None


@dataclass(frozen=True, kw_only=True)
class VerifiedCitation:
    """经物理边界校验后的生成结果溯源引用（兼容 Pydantic AI 原生 Citation 模型）。"""

    raw_tag: str
    target_kind: CitationTargetKind
    target_id: str
    locator: CitationLocator
    excerpt: str
    anchor: ContentCitationAnchor
    source: DocumentCitationSource
    verified: bool
    verification_reason: str = "ok"


class AttachmentRegistryPort(Protocol):
    """附件与内部产物只读查询协议（供溯源校验器与工具集解耦调用）。"""

    def get_attachment(self, attachment_id: str) -> AttachmentRef | None: ...

    def get_document_pages(self, attachment_id: str) -> tuple[str, ...] | None: ...

    def get_artifact(self, artifact_id: str, *, scope: str = "global") -> ArtifactRef | None: ...

    def list_attachments(self) -> list[AttachmentRef]: ...

    def list_artifacts(self, *, scope: str = "global") -> Mapping[str, ArtifactRef]: ...
