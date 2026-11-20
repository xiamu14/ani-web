"""Role 任务轨迹记忆提炼器: 从真实 ToolCallPart/ToolReturnPart 提取 100% 物理落地的 Observation 与 Summary。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic_ai.messages import ModelMessage, ToolCallPart

from agent.memory.anchors import (
    extract_line_anchors_from_content,
    index_file_lines,
    verify_line_anchors_with_index,
)
from agent.memory.budget import sanitize_and_redact_text, truncate_field_60_30_10
from agent.memory.ports import (
    CreateObservationInput,
    CreateSessionSummaryInput,
    LineAnchor,
    MemoryStorePort,
    ObservationType,
    SaveObservationResult,
    SessionSummaryRecord,
)
from agent.memory.workspace_io import read_workspace_text, resolve_workspace_path

_READ_TOOLS: frozenset[str] = frozenset(
    {"read_file", "file_info", "list_directory", "list_files", "search_files", "find_files", "grep"}
)
_WRITE_TOOLS: frozenset[str] = frozenset({"edit_file", "write_file", "create_directory"})
_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "target_file", "directory", "dir_path")


def _parse_call_args(call: ToolCallPart) -> dict[str, object]:
    if isinstance(call.args, dict):
        return cast(dict[str, object], call.args)
    if isinstance(call.args, str) and call.args.strip().startswith("{"):
        try:
            loaded: object = json.loads(call.args)  # pyright: ignore[reportAny]
            if isinstance(loaded, dict):
                return cast(dict[str, object], loaded)
        except json.JSONDecodeError:
            return {}
    return {}


def _first_meaningful_line(text: str, fallback: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#*- ").strip()
        if len(cleaned) >= 6:
            return cleaned[:120]
    return fallback[:120]


class TrajectoryMemoryExtractor:
    """从完成态 Role 任务轨迹提取 100% 工具实证的 Observation 与 SessionSummary 并落库。"""

    def __init__(self, store: MemoryStorePort) -> None:
        self._store: MemoryStorePort = store

    async def extract_and_store(
        self,
        *,
        project_key: str,
        conversation_id: str,
        task_id: str,
        role_name: str,
        mandate: str,
        output: str,
        messages: Sequence[ModelMessage],
        workspace_root: str | Path | None = None,
        discovery_tokens: int = 0,
    ) -> tuple[SaveObservationResult | None, SessionSummaryRecord]:
        ws_path = Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
        files_read: list[str] = []
        files_modified: list[str] = []
        edit_snippets_by_file: dict[str, str] = {}
        tools_used: list[str] = []

        for msg in messages:
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    if part.tool_name not in tools_used:
                        tools_used.append(part.tool_name)
                    args = _parse_call_args(part)
                    raw_p = next(
                        (
                            str(args[k]).strip()
                            for k in _PATH_KEYS
                            if isinstance(args.get(k), str) and str(args[k]).strip()
                        ),
                        None,
                    )
                    if raw_p is None:
                        continue
                    rel_p, _ = resolve_workspace_path(raw_p, ws_path)
                    if part.tool_name in _WRITE_TOOLS:
                        if rel_p not in files_modified:
                            files_modified.append(rel_p)
                        snippet = (
                            args.get("replacement")
                            or args.get("new_str")
                            or args.get("new_string")
                            or args.get("content")
                        )
                        if isinstance(snippet, str) and snippet.strip():
                            edit_snippets_by_file[rel_p] = snippet
                    elif part.tool_name in _READ_TOOLS and rel_p not in files_read:
                        files_read.append(rel_p)

        anchors: list[LineAnchor] = []
        file_hashes: dict[str, str] = {}
        target_files_for_anchors = files_modified if files_modified else files_read[:2]
        for rel_p in target_files_for_anchors:
            content = read_workspace_text(ws_path, rel_p)
            if content is None:
                continue
            file_hashes[rel_p] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            extracted = extract_line_anchors_from_content(
                rel_p,
                content,
                edited_snippet=edit_snippets_by_file.get(rel_p),
            )
            for anc in extracted:
                if len(anchors) < 3:
                    anchors.append(anc)

        clean_output = sanitize_and_redact_text(output).strip()
        clean_mandate = sanitize_and_redact_text(mandate).strip()

        summary = await self._store.save_summary(
            CreateSessionSummaryInput(
                project_key=project_key,
                conversation_id=conversation_id,
                task_id=task_id,
                request=clean_mandate[:1000],
                investigated=", ".join(files_read[:10]) or "(none)",
                learned=_first_meaningful_line(clean_output, clean_mandate),
                completed=truncate_field_60_30_10(clean_output, max_chars=2_000),
                files_read=tuple(files_read),
                files_edited=tuple(files_modified),
                discovery_tokens=discovery_tokens,
            )
        )

        if not files_read and not files_modified and not tools_used:
            return None, summary

        obs_type: ObservationType = "change" if files_modified else "discovery"
        title = _first_meaningful_line(clean_output, f"{role_name}: {clean_mandate}")
        facts: list[str] = []
        if files_modified:
            facts.append(f"Modified files: {', '.join(files_modified[:8])}")
        if files_read:
            facts.append(f"Inspected files: {', '.join(files_read[:8])}")
        if tools_used:
            facts.append(f"Tools executed: {', '.join(tools_used[:8])}")

        saved_obs = await self._store.save_observation(
            CreateObservationInput(
                project_key=project_key,
                conversation_id=conversation_id,
                role_name=role_name,
                type=obs_type,
                title=title,
                subtitle=f"Task {task_id} (@{role_name})",
                facts=tuple(facts),
                narrative=truncate_field_60_30_10(clean_output, max_chars=2_000),
                concepts=("what-changed",) if files_modified else ("how-it-works",),
                files_read=tuple(files_read),
                files_modified=tuple(files_modified),
                line_anchors=tuple(anchors),
                file_hashes=file_hashes,
                provenance="verified_by_tool",
                discovery_tokens=discovery_tokens,
            )
        )

        # 对所有被修改文件执行写穿失效检查：若旧观察在该文件上的行锚点已全部消失，自动标记 superseded_by
        for mod_path in files_modified:
            current_content = read_workspace_text(ws_path, mod_path)
            if current_content is None:
                continue
            file_idx = index_file_lines(current_content)
            prior_obs = await self._store.list_observations_for_file(
                project_key=project_key,
                file_path=mod_path,
                limit=50,
                include_superseded=False,
            )
            stale_ids = [
                old.id
                for old in prior_obs
                if old.id != saved_obs.record.id
                and (
                    (
                        old.conversation_id == conversation_id
                        and old.title.startswith(f"Modified {mod_path} via ")
                    )
                    or (
                        (ver := verify_line_anchors_with_index(old.line_anchors, mod_path, file_idx))
                        is not None
                        and ver.state == "changed"
                    )
                )
            ]
            if stale_ids:
                await self._store.mark_superseded(
                    project_key=project_key,
                    observation_ids=stale_ids,
                    superseded_by=saved_obs.record.id,
                )

        return saved_obs, summary
