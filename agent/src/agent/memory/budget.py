"""记忆预算裁剪与 L0/L1/L2 渲染层: 60/30/10 省略截断、WorkState 折叠与防过期标签渲染（零 IO）。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from agent.memory.anchors import format_line_anchor_suffix
from agent.memory.ports import (
    FoldedWorkStateTask,
    LineAnchorVerification,
    ObservationRecord,
    ObservationType,
    SessionSummaryRecord,
    WorkStateEntryRecord,
    WorkStateStatus,
)

OBS_FIELD_MAX_CHARS = 16_000
OBS_FIELD_HEAD_RATIO = 0.6
OBS_FIELD_TAIL_RATIO = 0.3
DEFAULT_CONTEXT_MAX_CHARS = 10_000
_CLOSED_STATUSES: frozenset[WorkStateStatus] = frozenset({"done", "cancelled"})
_PRIVATE_TAG_RE = re.compile(r"<private\b[^>]*>[\s\S]*?</private>", re.IGNORECASE)
_DATA_URI_BASE64_RE = re.compile(r"data:[a-zA-Z0-9.+/-]+;base64,[A-Za-z0-9+/=]{128,}")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|authorization|bearer|access[_-]?token)\b"
    + r"(\s*[:=]\s*['\"]?)([A-Za-z0-9._~+/=-]{8,})"
)
_TYPE_ICONS: Mapping[ObservationType, str] = {
    "decision": "⚖️",
    "bugfix": "🔴",
    "feature": "🟣",
    "refactor": "🔄",
    "discovery": "🔵",
    "change": "✅",
}


def sanitize_and_redact_text(text: str) -> str:
    """剥离 <private> 标签、Base64 媒体载荷并脱敏常见密钥串。"""
    cleaned = _PRIVATE_TAG_RE.sub("[REDACTED_PRIVATE]", text)
    cleaned = _DATA_URI_BASE64_RE.sub("[ELIDED_BASE64_MEDIA]", cleaned)
    return _SECRET_KV_RE.sub(r"\1\2[REDACTED_SECRET]", cleaned)


def truncate_field_60_30_10(
    text: str,
    *,
    max_chars: int = OBS_FIELD_MAX_CHARS,
) -> str:
    """按 60% Head + <elided> + 30% Tail 裁剪超长文本，先执行隐私与 Base64 清洗。"""
    cleaned = sanitize_and_redact_text(text)
    if len(cleaned) <= max_chars or max_chars <= 0:
        return cleaned
    head_len = max(1, int(max_chars * OBS_FIELD_HEAD_RATIO))
    tail_len = max(1, int(max_chars * OBS_FIELD_TAIL_RATIO))
    elided_count = max(0, len(cleaned) - head_len - tail_len)
    marker = f"\n<elided chars=\"{elided_count}\" />\n"
    return f"{cleaned[:head_len]}{marker}{cleaned[-tail_len:]}"


def fold_work_state_list(
    entries: Sequence[WorkStateEntryRecord],
    *,
    include_closed: bool = False,
) -> list[FoldedWorkStateTask]:
    """按写入顺序折叠 WorkState 账本：最新字段覆盖旧值，None 清除键，done/cancelled 默认隐藏。"""
    ordered = sorted(entries, key=lambda e: (e.created_at_epoch, e.id))
    by_key: dict[tuple[str, str], tuple[WorkStateStatus, dict[str, str], int]] = {}
    for entry in ordered:
        key = (entry.list_name, entry.task_name)
        _, prev_fields, _ = by_key.get(key, (entry.status, {}, entry.created_at_epoch))
        merged = dict(prev_fields)
        for field_key, field_val in entry.fields.items():
            if field_val is None:
                _ = merged.pop(field_key, None)
            else:
                merged[field_key] = field_val
        by_key[key] = (entry.status, merged, entry.created_at_epoch)

    folded: list[FoldedWorkStateTask] = []
    for (list_name, task_name), (status, fields, updated_at) in by_key.items():
        if not include_closed and status in _CLOSED_STATUSES:
            continue
        folded.append(
            FoldedWorkStateTask(
                list_name=list_name,
                task_name=task_name,
                status=status,
                fields=fields,
                updated_at_epoch=updated_at,
            )
        )
    folded.sort(key=lambda item: item.updated_at_epoch, reverse=True)
    return folded


def _format_utc_day(epoch: int) -> str:
    return datetime.fromtimestamp(max(0, epoch), tz=UTC).strftime("%Y-%m-%d")


def _format_utc_time(epoch: int) -> str:
    return datetime.fromtimestamp(max(0, epoch), tz=UTC).strftime("%H:%M")


def format_observation_timeline_row(
    obs: ObservationRecord,
    *,
    verification: LineAnchorVerification | None = None,
    include_facts: bool = False,
) -> str:
    """渲染单条 Observation 为 L1/L2 时间线行（过期锚点强制冠以 [STALE - CODE CHANGED]）。"""
    icon = _TYPE_ICONS.get(obs.type, "🔹")
    time_str = _format_utc_time(obs.created_at_epoch)
    clean_title = " ".join(obs.title.split())[:120]
    stale_prefix = (
        "[STALE - CODE CHANGED] "
        if verification is not None and verification.state == "changed"
        else ""
    )
    anchor_suffix = format_line_anchor_suffix(verification)
    head_line = f"#{obs.id} {time_str} {icon} [{obs.type}] {stale_prefix}{clean_title}{anchor_suffix}"
    if not include_facts or not obs.facts:
        return head_line
    fact_lines = [f"  - {fact}" for fact in obs.facts[:5]]
    return "\n".join([head_line, *fact_lines])


def render_l0_l1_context(
    *,
    observations: Sequence[ObservationRecord],
    summaries: Sequence[SessionSummaryRecord] = (),
    work_states: Sequence[WorkStateEntryRecord] = (),
    full_observation_count: int = 3,
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    anchor_verifications: Mapping[int, LineAnchorVerification] | None = None,
) -> str:
    """构建会话/Role 启动时的 L0 (WorkState + Latest Summary) + L1 (紧凑时间线) 记忆块。

    当超出 max_chars 预算时，从最旧的 L1 观察开始整条丢弃，保证输出结构完整。
    """
    active_obs = [o for o in observations if o.superseded_by is None]
    folded_tasks = fold_work_state_list(work_states, include_closed=False)
    latest_summary = (
        max(summaries, key=lambda s: (s.created_at_epoch, s.id)) if summaries else None
    )
    if not active_obs and not folded_tasks and latest_summary is None:
        return ""

    header_lines: list[str] = [
        "## Workspace Memory (L0 State & L1 Timeline Index)",
        (
            "NOTE: Entries marked `[STALE - CODE CHANGED]` or `(code changed since)` reflect "
            "past state; always verify current code with `read_file` before relying on exact numbers or lines."
        ),
    ]

    l0_lines: list[str] = []
    if folded_tasks:
        l0_lines.append("### Open Work State (L0)")
        for task in folded_tasks[:15]:
            kv = ", ".join(f"{k}={v}" for k, v in task.fields.items())
            detail = f" ({kv})" if kv else ""
            l0_lines.append(f"- [{task.status}] {task.list_name}/{task.task_name}{detail}")

    if latest_summary is not None:
        l0_lines.append("### Latest Session Summary (L0)")
        if latest_summary.request:
            l0_lines.append(f"- Request: {latest_summary.request}")
        if latest_summary.completed:
            l0_lines.append(f"- Completed: {latest_summary.completed}")
        if latest_summary.learned:
            l0_lines.append(f"- Learned: {latest_summary.learned}")
        if latest_summary.next_steps:
            l0_lines.append(f"- Next steps: {latest_summary.next_steps}")

    newest_first = sorted(active_obs, key=lambda o: (o.created_at_epoch, o.id), reverse=True)
    full_ids = {o.id for o in newest_first[: max(0, full_observation_count)]}

    # 从最新项开始纳入预算，再按时间升序（旧->新）渲染，方便后续增量追加
    kept_Newest_first: list[ObservationRecord] = []
    base_text = "\n".join([*header_lines, *l0_lines])
    running_chars = len(base_text) + 64
    verifications = anchor_verifications or {}

    for obs in newest_first:
        row_text = format_observation_timeline_row(
            obs,
            verification=verifications.get(obs.id),
            include_facts=obs.id in full_ids,
        )
        projected = running_chars + len(row_text) + 16
        if projected > max_chars and kept_Newest_first:
            break
        kept_Newest_first.append(obs)
        running_chars = projected

    chronological = list(reversed(kept_Newest_first))
    l1_lines: list[str] = []
    if chronological:
        l1_lines.append("### Observation Timeline (L1 — use `get_observations([id])` for details)")
        current_day = ""
        for obs in chronological:
            day = _format_utc_day(obs.created_at_epoch)
            if day != current_day:
                l1_lines.append(f"#### {day}")
                current_day = day
            l1_lines.append(
                format_observation_timeline_row(
                    obs,
                    verification=verifications.get(obs.id),
                    include_facts=obs.id in full_ids,
                )
            )

    return "\n".join([*header_lines, *l0_lines, *l1_lines]).strip()
