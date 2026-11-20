"""
物理行锚点（LineAnchor）纯函数层: 从真实工具报文提取唯一代码行与读取时实时重定位（零 IO）。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agent.memory.ports import LineAnchor, LineAnchorState, LineAnchorVerification

MAX_LINE_ANCHORS_PER_OBSERVATION = 3
MIN_DISTINCTIVE_ALNUM_CHARS = 8
MAX_LINE_ANCHOR_CHARS = 400
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_LEADING_DOT_SLASH_RE = re.compile(r"^(?:\./)+")


def normalize_anchor_path(file_path: str) -> str:
    """统一路径分隔符与前缀以便跨调用稳定比对。"""
    normalized = file_path.replace("\\", "/").strip()
    return _LEADING_DOT_SLASH_RE.sub("", normalized)


def squash_whitespace(line: str) -> str:
    """将连续空白折叠为单空格并去除首尾空白。"""
    return _WHITESPACE_RUN_RE.sub(" ", line).strip()


def is_distinctive_line(line: str) -> bool:
    """判断单行代码是否具有足够特征度（排除闭合括号/短关键字与超长生成行）。"""
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_LINE_ANCHOR_CHARS:
        return False
    alnum_count = sum(1 for ch in stripped if ch.isalnum())
    return alnum_count >= MIN_DISTINCTIVE_ALNUM_CHARS


@dataclass(frozen=True)
class FileLineIndex:
    """文件行双向索引：支持精确文本查找与空白折叠文本查找（1-based 行号）。"""

    lines: tuple[str, ...]
    exact_map: dict[str, tuple[int, ...]]
    squashed_map: dict[str, tuple[int, ...]]

    def lines_with_text(self, text: str) -> tuple[int, ...]:
        return self.exact_map.get(text, ())

    def lines_with_squashed_text(self, text: str) -> tuple[int, ...]:
        return self.squashed_map.get(squash_whitespace(text), ())


def index_file_lines(content: str) -> FileLineIndex:
    """构建文件内容 1-based 行索引。"""
    raw_lines = tuple(content.splitlines())
    exact_lists: dict[str, list[int]] = {}
    squashed_lists: dict[str, list[int]] = {}
    for idx, line in enumerate(raw_lines, start=1):
        exact_lists.setdefault(line, []).append(idx)
        sq = squash_whitespace(line)
        if sq:
            squashed_lists.setdefault(sq, []).append(idx)
    return FileLineIndex(
        lines=raw_lines,
        exact_map={k: tuple(v) for k, v in exact_lists.items()},
        squashed_map={k: tuple(v) for k, v in squashed_lists.items()},
    )


def find_line_anchor(anchor: LineAnchor, index: FileLineIndex) -> int | None:
    """在当前文件索引中重定位锚点行号。

    优先精确唯一命中，其次空白折叠唯一命中；若出现 0 次或 >=2 次均返回 None，绝不猜测行号。
    """
    exact_hits = index.lines_with_text(anchor.text)
    if len(exact_hits) == 1:
        return exact_hits[0]
    if not squash_whitespace(anchor.text):
        return None
    squashed_hits = index.lines_with_squashed_text(anchor.text)
    if len(squashed_hits) == 1:
        return squashed_hits[0]
    return None


def verify_line_anchors_with_index(
    anchors: Sequence[LineAnchor],
    target_path: str,
    index: FileLineIndex,
) -> LineAnchorVerification | None:
    """使用预构建的 FileLineIndex 校验某条 Observation 在 target_path 上的全部行锚点（避免重复切分文件）。"""
    norm_target = normalize_anchor_path(target_path)
    matching = [a for a in anchors if normalize_anchor_path(a.path) == norm_target]
    if not matching:
        return None

    found_lines: list[int] = []
    missing = 0
    for anchor in matching:
        relocated = find_line_anchor(anchor, index)
        if relocated is not None:
            if relocated not in found_lines:
                found_lines.append(relocated)
        else:
            missing += 1

    found_lines.sort()
    state: LineAnchorState
    if not found_lines:
        state = "changed"
    elif missing > 0:
        state = "partly-changed"
    else:
        state = "found"
    return LineAnchorVerification(
        state=state, lines=tuple(found_lines), missing=missing
    )


def verify_line_anchors(
    anchors: Sequence[LineAnchor],
    target_path: str,
    current_content: str,
) -> LineAnchorVerification | None:
    """对照当前文件内容校验某条 Observation 在 target_path 上的全部行锚点。"""
    norm_target = normalize_anchor_path(target_path)
    if not any(normalize_anchor_path(a.path) == norm_target for a in anchors):
        return None
    return verify_line_anchors_with_index(anchors, norm_target, index_file_lines(current_content))


def format_line_anchor_suffix(verification: LineAnchorVerification | None) -> str:
    """将行锚点校验状态格式化为时间线行尾标记。"""
    if verification is None:
        return ""
    if verification.state == "changed":
        return " (code changed since)"
    joined = ",".join(f"L{line_no}" for line_no in verification.lines)
    if verification.state == "partly-changed":
        return f" @{joined} (some code changed since)"
    return f" @{joined}"


def extract_line_anchors_from_content(
    path: str,
    file_content: str,
    *,
    candidate_lines: Sequence[int] | None = None,
    edited_snippet: str | None = None,
    max_anchors: int = MAX_LINE_ANCHORS_PER_OBSERVATION,
) -> tuple[LineAnchor, ...]:
    """从真实文件或编辑片段中提取唯一特征行锚点（精确唯一且空白折叠唯一）。"""
    norm_path = normalize_anchor_path(path)
    index = index_file_lines(file_content)
    total_lines = len(index.lines)
    if total_lines == 0 or max_anchors <= 0:
        return ()

    line_numbers: list[int] = []
    if edited_snippet:
        for snippet_line in edited_snippet.splitlines():
            sq = squash_whitespace(snippet_line)
            if not sq:
                continue
            hits = index.lines_with_squashed_text(snippet_line)
            if len(hits) == 1 and hits[0] not in line_numbers:
                line_numbers.append(hits[0])

    if candidate_lines is not None:
        for ln in candidate_lines:
            if 1 <= ln <= total_lines and ln not in line_numbers:
                line_numbers.append(ln)

    if not line_numbers:
        line_numbers = list(range(1, min(total_lines, 50) + 1))

    anchors: list[LineAnchor] = []
    for ln in line_numbers:
        raw_text = index.lines[ln - 1]
        if not is_distinctive_line(raw_text):
            continue
        if len(index.lines_with_text(raw_text)) != 1:
            continue
        if len(index.lines_with_squashed_text(raw_text)) != 1:
            continue
        anchors.append(LineAnchor(path=norm_path, line=ln, text=raw_text))
        if len(anchors) >= max_anchors:
            break
    return tuple(anchors)
