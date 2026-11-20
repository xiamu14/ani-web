"""多类型文件附件引用传输（Attachment）、内部产物快速引用（Artifact）与生成结果溯源包。"""

from agent.attachments.citations import (
    extract_and_verify_citations,
    parse_bbox,
    parse_time_range_sec,
    parse_timestamp_sec,
    resolve_artifact_key_path,
)
from agent.attachments.ports import (
    ArtifactRef,
    AttachmentKind,
    AttachmentRef,
    AttachmentRegistryPort,
    CitationLocator,
    CitationTargetKind,
    VerifiedCitation,
    infer_attachment_kind,
)
from agent.attachments.registry import AttachmentRegistry
from agent.attachments.toolset import build_attachment_toolset

__all__ = [
    "ArtifactRef",
    "AttachmentKind",
    "AttachmentRef",
    "AttachmentRegistry",
    "AttachmentRegistryPort",
    "CitationLocator",
    "CitationTargetKind",
    "VerifiedCitation",
    "build_attachment_toolset",
    "extract_and_verify_citations",
    "infer_attachment_kind",
    "parse_bbox",
    "parse_time_range_sec",
    "parse_timestamp_sec",
    "resolve_artifact_key_path",
]
