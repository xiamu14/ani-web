"""SQLModel 记忆表序列化与反序列化纯辅助函数（供 memory_store 内部使用，保持单文件 < 500 行）。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from agent.db.models import ObservationRow, SessionSummaryRow, WorkStateRow
from agent.memory.anchors import normalize_anchor_path
from agent.memory.ports import (
    LineAnchor,
    MemoryProvenance,
    ObservationRecord,
    ObservationType,
    SessionSummaryRecord,
    WorkStateEntryRecord,
    WorkStateStatus,
)

_FTS_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def parse_str_tuple(raw_json: str) -> tuple[str, ...]:
    try:
        parsed: object = json.loads(raw_json)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    items = cast(list[object], parsed)
    return tuple(str(item).strip() for item in items if str(item).strip())


def parse_str_map(raw_json: str) -> dict[str, str]:
    try:
        parsed: object = json.loads(raw_json)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    raw_map = cast(dict[object, object], parsed)
    return {str(k): str(v) for k, v in raw_map.items() if v is not None}


def parse_nullable_str_map(raw_json: str) -> dict[str, str | None]:
    try:
        parsed: object = json.loads(raw_json)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    raw_map = cast(dict[object, object], parsed)
    return {str(k): (None if v is None else str(v)) for k, v in raw_map.items()}


def parse_anchors(raw_json: str) -> tuple[LineAnchor, ...]:
    try:
        parsed: object = json.loads(raw_json)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    anchors: list[LineAnchor] = []
    for raw_item in cast(list[object], parsed):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, object], raw_item)
        path = str(item.get("path", "")).strip()
        line_val = item.get("line")
        text_val = str(item.get("text", ""))
        if path and isinstance(line_val, int) and text_val:
            anchors.append(LineAnchor(path=normalize_anchor_path(path), line=line_val, text=text_val))
    return tuple(anchors)


def dump_anchors(anchors: Sequence[LineAnchor]) -> str:
    payload = [
        {"path": normalize_anchor_path(a.path), "line": a.line, "text": a.text}
        for a in anchors
    ]
    return json.dumps(payload, ensure_ascii=False)


def merge_unique_strs(existing: Sequence[str], added: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for val in (*existing, *added):
        cleaned = val.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


def row_to_observation(row: ObservationRow) -> ObservationRecord:
    return ObservationRecord(
        id=row.id or 0,
        project_key=row.project_key,
        conversation_id=row.conversation_id,
        role_name=row.role_name,
        type=cast(ObservationType, row.type),
        title=row.title,
        subtitle=row.subtitle,
        facts=parse_str_tuple(row.facts_json),
        narrative=row.narrative,
        concepts=parse_str_tuple(row.concepts_json),
        files_read=parse_str_tuple(row.files_read_json),
        files_modified=parse_str_tuple(row.files_modified_json),
        line_anchors=parse_anchors(row.line_anchors_json),
        file_hashes=parse_str_map(row.file_hashes_json),
        provenance=cast(MemoryProvenance, row.provenance),
        discovery_tokens=row.discovery_tokens,
        reinforcement_dates=parse_str_tuple(row.reinforcement_dates_json),
        superseded_by=row.superseded_by,
        created_at_epoch=row.created_at_epoch,
    )


def row_to_summary(row: SessionSummaryRow) -> SessionSummaryRecord:
    return SessionSummaryRecord(
        id=row.id or 0,
        project_key=row.project_key,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        request=row.request,
        investigated=row.investigated,
        learned=row.learned,
        completed=row.completed,
        next_steps=row.next_steps,
        notes=row.notes,
        files_read=parse_str_tuple(row.files_read_json),
        files_edited=parse_str_tuple(row.files_edited_json),
        discovery_tokens=row.discovery_tokens,
        created_at_epoch=row.created_at_epoch,
    )


def row_to_work_state(row: WorkStateRow) -> WorkStateEntryRecord:
    return WorkStateEntryRecord(
        id=row.id or 0,
        project_key=row.project_key,
        list_name=row.list_name,
        task_name=row.task_name,
        fields=parse_nullable_str_map(row.fields_json),
        status=cast(WorkStateStatus, row.status),
        created_at_epoch=row.created_at_epoch,
    )


def build_fts_match_expr(raw_query: str) -> str:
    tokens = [m.group(0) for m in _FTS_WORD_RE.finditer(raw_query)]
    if not tokens:
        return ""
    return " OR ".join(f'"{tok}"' for tok in tokens[:16])


def file_specificity_score(obs: ObservationRecord, target_norm_path: str) -> int:
    in_modified = any(normalize_anchor_path(p) == target_norm_path for p in obs.files_modified)
    total_files = len(obs.files_read) + len(obs.files_modified)
    score = 2 if in_modified else 0
    if total_files <= 3:
        score += 2
    elif total_files <= 8:
        score += 1
    return score
