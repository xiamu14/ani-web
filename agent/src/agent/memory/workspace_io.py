"""工作区文件路径解析与批量行锚点磁盘核验辅助层（带单次批量 FileLineIndex 缓存，避免重复读盘）。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agent.memory.anchors import (
    FileLineIndex,
    index_file_lines,
    normalize_anchor_path,
    verify_line_anchors_with_index,
)
from agent.memory.ports import LineAnchorVerification, ObservationRecord


def resolve_workspace_path(
    raw_path: str,
    workspace_root: Path | None,
) -> tuple[str, Path | None]:
    """将输入相对/绝对路径统一解析为 (规范化相对路径 rel_path, 磁盘绝对路径 disk_path)。"""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() and workspace_root is not None:
        disk = (workspace_root / candidate).resolve()
    elif candidate.is_absolute():
        disk = candidate.resolve()
    else:
        return normalize_anchor_path(raw_path), None

    if workspace_root is not None:
        try:
            rel = str(disk.relative_to(workspace_root.resolve()))
            return normalize_anchor_path(rel), disk
        except ValueError:
            pass
    return normalize_anchor_path(raw_path), disk


def read_workspace_text(
    workspace_root: Path | None,
    rel_or_abs: str,
) -> str | None:
    """从工作区安全读取 UTF-8 文本；若文件不存在或读取出错则返回 None。"""
    _, disk_path = resolve_workspace_path(rel_or_abs, workspace_root)
    if disk_path is None:
        return None
    try:
        if disk_path.is_file():
            return disk_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return None


def verify_observations_on_disk(
    observations: Sequence[ObservationRecord],
    workspace_root: Path | None,
) -> dict[int, LineAnchorVerification]:
    """批量核验一组 ObservationRecord 的行锚点，按文件缓存 FileLineIndex（同一文件只读盘 + 建索引 1 次）。"""
    if workspace_root is None:
        return {}
    index_cache: dict[str, FileLineIndex | None] = {}
    verifications: dict[int, LineAnchorVerification] = {}
    for obs in observations:
        if not obs.line_anchors:
            continue
        rel_path = normalize_anchor_path(obs.line_anchors[0].path)
        if rel_path not in index_cache:
            content = read_workspace_text(workspace_root, rel_path)
            index_cache[rel_path] = index_file_lines(content) if content is not None else None
        cached_idx = index_cache[rel_path]
        if cached_idx is None:
            verifications[obs.id] = LineAnchorVerification(
                state="changed",
                lines=(),
                missing=len(obs.line_anchors),
            )
        else:
            ver = verify_line_anchors_with_index(obs.line_anchors, rel_path, cached_idx)
            if ver is not None:
                verifications[obs.id] = ver
    return verifications
