"""L2 文件上下文记忆唤醒与写穿失效 Hook: 在 read_file / edit_file / write_file 时实时联动行锚点。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from agent.hooks.priority import HookPriority
from agent.memory.anchors import (
    extract_line_anchors_from_content,
    normalize_anchor_path,
    verify_line_anchors,
)
from agent.memory.budget import format_observation_timeline_row
from agent.memory.ports import (
    CreateObservationInput,
    LineAnchorVerification,
    MemoryStorePort,
    ObservationRecord,
)

FILE_CONTEXT_MIN_BYTES = 1_500
LARGE_FILE_HINT_BYTES = 32 * 1024
_READ_FILE_TOOLS: frozenset[str] = frozenset({"read_file", "search_files", "grep"})
_WRITE_FILE_TOOLS: frozenset[str] = frozenset({"edit_file", "write_file"})
_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "target_file")


def _extract_path_arg(args: dict[str, Any]) -> str | None:  # pyright: ignore[reportExplicitAny]
    for key in _PATH_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _resolve_disk_and_rel_path(
    raw_path: str,
    workspace_root: Path | None,
) -> tuple[Path, str]:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() and workspace_root is not None:
        disk_path = (workspace_root / candidate).resolve()
    else:
        disk_path = candidate.resolve()

    if workspace_root is not None:
        try:
            rel = str(disk_path.relative_to(workspace_root.resolve()))
            return disk_path, normalize_anchor_path(rel)
        except ValueError:
            pass
    return disk_path, normalize_anchor_path(raw_path)


def _format_l2_file_timeline(
    rel_path: str,
    file_size_bytes: int,
    observations: list[ObservationRecord],
    verifications: dict[int, LineAnchorVerification],
) -> str:
    has_changed = any(v.state == "changed" for v in verifications.values())
    has_found = any(v.state in ("found", "partly-changed") and v.lines for v in verifications.values())

    lines: list[str] = [
        f"\n\n--- [L2 File Memory Timeline: {rel_path}] ---",
        "- Need full details on an observation? Call `get_observations(ids=[...])`.",
    ]
    if file_size_bytes >= LARGE_FILE_HINT_BYTES and has_found:
        lines.append(
            f"- Large file ({file_size_bytes} bytes >= 32KB): use the `@L` line numbers below "
            + "for targeted line-range reads instead of re-reading the entire file."
        )
    if has_changed:
        lines.append(
            "- WARNING: `[STALE - CODE CHANGED]` / `(code changed since)` means the file was "
            + "modified after that observation. Always trust the current file content over stale memory."
        )

    chronological = sorted(observations, key=lambda o: (o.created_at_epoch, o.id))
    for obs in chronological:
        lines.append(
            format_observation_timeline_row(
                obs,
                verification=verifications.get(obs.id),
                include_facts=False,
            )
        )
    return "\n".join(lines)


class FileMemoryContextHook(AbstractCapability[object]):
    """在 read_file 时按需注入 L2 文件历史观察时间线（含实时行锚点重算），在 edit/write 时写穿失效旧记忆。"""

    hook_priority: int = int(HookPriority.DEFAULT)

    def __init__(
        self,
        *,
        store: MemoryStorePort,
        project_key: str,
        workspace_root: str | Path | None = None,
        conversation_id: str = "default",
        role_name: str = "role",
        min_file_bytes: int = FILE_CONTEXT_MIN_BYTES,
        auto_record_edits: bool = True,
    ) -> None:
        self._store: MemoryStorePort = store
        self._project_key: str = project_key
        self._workspace_root: Path | None = (
            Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
        )
        self._conversation_id: str = conversation_id
        self._role_name: str = role_name
        self._min_file_bytes: int = min_file_bytes
        self._auto_record_edits: bool = auto_record_edits
        self._injected_files: set[str] = set()

    @override
    async def after_tool_execute(  # pyright: ignore[reportAny]
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
        result: Any,  # pyright: ignore[reportExplicitAny, reportAny]
    ) -> Any:  # pyright: ignore[reportExplicitAny]
        _ = (ctx, tool_def)
        raw_path = _extract_path_arg(args)
        if raw_path is None:
            return result  # pyright: ignore[reportAny]

        disk_path, rel_path = _resolve_disk_and_rel_path(raw_path, self._workspace_root)
        if call.tool_name in _READ_FILE_TOOLS and isinstance(result, str):
            return await self._handle_read_file(disk_path, rel_path, result)
        if call.tool_name in _WRITE_FILE_TOOLS:
            await self._handle_write_file(call.tool_name, disk_path, rel_path, args)
        return result  # pyright: ignore[reportAny]

    async def _handle_read_file(
        self,
        disk_path: Path,
        rel_path: str,
        result_text: str,
    ) -> str:
        if rel_path in self._injected_files:
            return result_text
        try:
            if not disk_path.is_file():
                return result_text
            file_size = disk_path.stat().st_size
            if file_size < self._min_file_bytes:
                return result_text
            current_content = disk_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return result_text

        history = await self._store.list_observations_for_file(
            project_key=self._project_key,
            file_path=rel_path,
            limit=10,
            include_superseded=False,
        )
        if not history:
            return result_text

        verifications: dict[int, LineAnchorVerification] = {}
        for obs in history:
            ver = verify_line_anchors(obs.line_anchors, rel_path, current_content)
            if ver is not None:
                verifications[obs.id] = ver

        self._injected_files.add(rel_path)
        timeline_block = _format_l2_file_timeline(rel_path, file_size, history, verifications)
        return f"{result_text}{timeline_block}"

    async def _handle_write_file(
        self,
        tool_name: str,
        disk_path: Path,
        rel_path: str,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
    ) -> None:
        # 文件发生变更后允许同一会话后续 read_file 重新刷出最新锚点状态
        self._injected_files.discard(rel_path)
        try:
            if not disk_path.is_file():
                return
            current_content = disk_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        existing = await self._store.list_observations_for_file(
            project_key=self._project_key,
            file_path=rel_path,
            limit=50,
            include_superseded=False,
        )
        invalidated_ids: list[int] = []
        for obs in existing:
            ver = verify_line_anchors(obs.line_anchors, rel_path, current_content)
            if ver is not None and ver.state == "changed":
                invalidated_ids.append(obs.id)

        if not self._auto_record_edits and not invalidated_ids:
            return

        snippet_raw = (
            args.get("replacement")
            or args.get("new_str")
            or args.get("new_string")
            or args.get("content")
        )
        snippet = snippet_raw if isinstance(snippet_raw, str) else None
        anchors = extract_line_anchors_from_content(
            rel_path,
            current_content,
            edited_snippet=snippet,
        )
        file_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()

        if self._auto_record_edits:
            saved = await self._store.save_observation(
                CreateObservationInput(
                    project_key=self._project_key,
                    conversation_id=self._conversation_id,
                    role_name=self._role_name,
                    type="change",
                    title=f"Modified {rel_path} via {tool_name}",
                    facts=(f"Updated {rel_path} (sha256={file_hash[:12]})",),
                    concepts=("what-changed",),
                    files_modified=(rel_path,),
                    line_anchors=anchors,
                    file_hashes={rel_path: file_hash},
                    provenance="verified_by_tool",
                )
            )
            if invalidated_ids:
                await self._store.mark_superseded(
                    project_key=self._project_key,
                    observation_ids=invalidated_ids,
                    superseded_by=saved.record.id,
                )
