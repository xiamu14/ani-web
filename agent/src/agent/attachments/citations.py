"""生成结果溯源解析与物理边界防伪校验器（Layer 0 纯函数层）。

支持 4 类统一溯源语法并映射为 Pydantic AI 原生 ContentCitationAnchor + DocumentCitationSource：
1. 文档页码与精确行范围: [[cite:att_xxx#page=4&line=12-18 | "摘录"]]
2. 音视频时间轴区间:     [[cite:att_xxx#t=01:15-01:42 | "关键帧/语音描述"]]
3. 图像归一化区域框:     [[cite:att_xxx#bbox=0.15,0.20,0.85,0.45 | "区域描述"]]
4. 内部产物/状态字段:    [[cite:art:var_key#key=pricing.min_charge | "24.75"]]（兼容 state: 别名）
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from pydantic_ai.messages import ContentCitationAnchor, DocumentCitationSource

from agent.attachments.ports import (
    ArtifactRef,
    AttachmentRef,
    AttachmentRegistryPort,
    CitationLocator,
    CitationTargetKind,
    VerifiedCitation,
)

_CITE_TAG_RE = re.compile(
    r"\[\[cite:(?P<target>[A-Za-z0-9_:.-]+)(?:#(?P<frag>[^\]|]+))?(?:\s*\|\s*(?P<excerpt>[^\]]+))?\]\]"
)


def parse_timestamp_sec(raw: str) -> float | None:
    """将 'MM:SS'、'HH:MM:SS' 或纯秒数字符串解析为非负浮点秒数。"""
    cleaned = raw.strip().removesuffix("s")
    if not cleaned:
        return None
    if ":" in cleaned:
        parts = cleaned.split(":")
        if len(parts) not in (2, 3):
            return None
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if any(n < 0 for n in nums):
            return None
        if len(nums) == 2:
            return nums[0] * 60.0 + nums[1]
        return nums[0] * 3600.0 + nums[1] * 60.0 + nums[2]
    try:
        val = float(cleaned)
        return val if val >= 0.0 else None
    except ValueError:
        return None


def parse_time_range_sec(raw: str) -> tuple[float, float] | None:
    """解析 '01:15-01:42' 或单点 '45.0' 为 (start_sec, end_sec)。"""
    cleaned = raw.strip()
    if "-" in cleaned:
        left, right = cleaned.split("-", 1)
        start = parse_timestamp_sec(left)
        end = parse_timestamp_sec(right)
        if start is None or end is None or start > end:
            return None
        return (start, end)
    single = parse_timestamp_sec(cleaned)
    if single is None:
        return None
    return (single, single)


def parse_bbox(raw: str) -> tuple[float, float, float, float] | None:
    """解析 'x1,y1,x2,y2' 归一化坐标框，校验 0 <= x1 < x2 <= 1 且 0 <= y1 < y2 <= 1。"""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        return None
    if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
        return None
    return (x1, y1, x2, y2)


def _parse_fragment(fragment: str | None) -> tuple[CitationLocator, str | None]:
    if not fragment or not fragment.strip():
        return CitationLocator(), None
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    time_start: float | None = None
    time_end: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    artifact_key_path: str | None = None

    for raw_pair in fragment.strip().split("&"):
        pair = raw_pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            return CitationLocator(), f"Malformed locator fragment (expected key=value): {pair!r}"
        k, v = (s.strip() for s in pair.split("=", 1))
        if k == "page":
            try:
                page = int(v)
            except ValueError:
                return CitationLocator(), f"Invalid page number: {v!r}"
            if page < 1:
                return CitationLocator(), f"Page number must be >= 1, got {page}"
        elif k == "line":
            if "-" in v:
                l_str, r_str = v.split("-", 1)
                try:
                    line_start, line_end = int(l_str), int(r_str)
                except ValueError:
                    return CitationLocator(), f"Invalid line range: {v!r}"
            else:
                try:
                    line_start = int(v)
                    line_end = line_start
                except ValueError:
                    return CitationLocator(), f"Invalid line number: {v!r}"
            if line_start < 1 or line_end < line_start:
                return CitationLocator(), f"Line range out of order: {v!r}"
        elif k in ("t", "time"):
            tr = parse_time_range_sec(v)
            if tr is None:
                return CitationLocator(), f"Invalid timestamp range: {v!r}"
            time_start, time_end = tr
        elif k == "bbox":
            parsed_box = parse_bbox(v)
            if parsed_box is None:
                return (
                    CitationLocator(),
                    f"Invalid normalized bbox (expected 0<=x1<x2<=1, 0<=y1<y2<=1): {v!r}",
                )
            bbox = parsed_box
        elif k == "key":
            if not v:
                return CitationLocator(), "Empty artifact key path"
            artifact_key_path = v
        else:
            return CitationLocator(), f"Unknown locator key: {k!r}"

    return (
        CitationLocator(
            page=page,
            line_start=line_start,
            line_end=line_end,
            time_start_sec=time_start,
            time_end_sec=time_end,
            bbox=bbox,
            artifact_key_path=artifact_key_path,
        ),
        None,
    )


def resolve_artifact_key_path(root_val: object, key_path: str | None) -> tuple[bool, object]:
    """在嵌套字典/列表中按点号路径（如 'pricing.min_charge'）解析内部产物字段。"""
    if not key_path:
        return True, root_val
    curr: object = root_val
    for seg in key_path.split("."):
        if isinstance(curr, Mapping):
            typed_map = cast(Mapping[str, object], curr)
            if seg not in typed_map:
                return False, None
            curr = typed_map[seg]
        elif isinstance(curr, (list, tuple)) and seg.isdigit():
            idx = int(seg)
            seq = cast(list[object] | tuple[object, ...], curr)
            if idx < 0 or idx >= len(seq):
                return False, None
            curr = seq[idx]
        else:
            return False, None
    return True, curr


def _verify_attachment_citation(
    att: AttachmentRef | None,
    pages: tuple[str, ...] | None,
    locator: CitationLocator,
    excerpt: str,
) -> tuple[bool, str]:
    if att is None:
        return False, "Attachment not found in AttachmentRegistry"

    if locator.page is not None:
        if att.page_count is not None and locator.page > att.page_count:
            return (
                False,
                f"Page {locator.page} exceeds document page_count={att.page_count}",
            )
        if pages is not None and 1 <= locator.page <= len(pages):
            page_text = pages[locator.page - 1]
            page_lines = page_text.splitlines()
            if locator.line_end is not None and locator.line_end > len(page_lines):
                return (
                    False,
                    f"Line {locator.line_end} exceeds page {locator.page} line count={len(page_lines)}",
                )
            if excerpt:
                if locator.line_start is not None and locator.line_end is not None:
                    line_slice = "\n".join(page_lines[locator.line_start - 1 : locator.line_end])
                    if excerpt not in line_slice:
                        return (
                            False,
                            f"Excerpt {excerpt!r} not found in page {locator.page} lines {locator.line_start}-{locator.line_end}",
                        )
                elif excerpt not in page_text:
                    return False, f"Excerpt {excerpt!r} not found on page {locator.page}"

    if (
        locator.time_end_sec is not None
        and att.duration_sec is not None
        and locator.time_end_sec > att.duration_sec + 1e-6
    ):
        return (
            False,
            f"Timestamp {locator.time_end_sec:.1f}s exceeds media duration={att.duration_sec:.1f}s",
        )

    if locator.bbox is not None and att.kind != "image":
        return False, f"Bounding box locator is only valid for image attachments, got kind={att.kind!r}"

    return True, "ok"


def _verify_artifact_citation(
    record: ArtifactRef | None,
    locator: CitationLocator,
    excerpt: str,
) -> tuple[bool, str]:
    if record is None:
        return False, "Artifact not found in AttachmentRegistry"
    found, resolved_val = resolve_artifact_key_path(record.value, locator.artifact_key_path)
    if not found:
        return (
            False,
            f"Key path {locator.artifact_key_path!r} not found in artifact {record.artifact_id!r}",
        )
    if excerpt and str(resolved_val) not in excerpt and excerpt not in str(resolved_val):
        return (
            False,
            f"Artifact value {resolved_val!r} does not match cited excerpt {excerpt!r}",
        )
    return True, "ok"


def extract_and_verify_citations(
    output_text: str,
    registry: AttachmentRegistryPort,
    *,
    artifact_scope: str = "global",
) -> list[VerifiedCitation]:
    """从生成输出中提取所有 [[cite:...]] 标签，核验物理边界并构建 Pydantic AI 标准溯源对象。"""
    results: list[VerifiedCitation] = []
    for match in _CITE_TAG_RE.finditer(output_text):
        raw_tag = match.group(0)
        raw_target = match.group("target").strip()
        raw_frag = match.group("frag")
        raw_excerpt = (match.group("excerpt") or "").strip().strip("\"'")

        anchor = ContentCitationAnchor(start=match.start(), end=match.end())
        locator, frag_err = _parse_fragment(raw_frag)

        is_artifact = raw_target.startswith(("artifact:", "art:", "state:"))
        if is_artifact:
            target_kind: CitationTargetKind = "artifact"
            target_id = (
                raw_target.removeprefix("artifact:")
                .removeprefix("art:")
                .removeprefix("state:")
                .strip()
            )
            art_rec = registry.get_artifact(target_id, scope=artifact_scope)
            if frag_err is not None:
                verified, reason = False, frag_err
            else:
                verified, reason = _verify_artifact_citation(art_rec, locator, raw_excerpt)
            title = f"artifact:{target_id}"
        else:
            target_kind = "attachment"
            target_id = raw_target.removeprefix("attachment:").removeprefix("att:").strip()
            att = registry.get_attachment(target_id)
            pages = registry.get_document_pages(target_id)
            if frag_err is not None:
                verified, reason = False, frag_err
            else:
                verified, reason = _verify_attachment_citation(att, pages, locator, raw_excerpt)
            title = att.title if att is not None else target_id

        excerpts = [raw_excerpt] if raw_excerpt else [raw_tag]
        source = DocumentCitationSource(
            document_id=target_id,
            title=title,
            excerpts=excerpts,
        )
        results.append(
            VerifiedCitation(
                raw_tag=raw_tag,
                target_kind=target_kind,
                target_id=target_id,
                locator=locator,
                excerpt=raw_excerpt,
                anchor=anchor,
                source=source,
                verified=verified,
                verification_reason=reason,
            )
        )
    return results
