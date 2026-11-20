"""L3 主动三步渐进检索工具集: memory_search -> memory_timeline -> get_observations + save_memory。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic_ai.toolsets import FunctionToolset

from agent.memory.anchors import (
    extract_line_anchors_from_content,
    format_line_anchor_suffix,
    normalize_anchor_path,
)
from agent.memory.budget import format_observation_timeline_row
from agent.memory.ports import (
    CreateObservationInput,
    LineAnchor,
    LineAnchorVerification,
    MemoryStorePort,
    ObservationRecord,
    ObservationType,
)
from agent.memory.workspace_io import read_workspace_text, verify_observations_on_disk

_VALID_OBS_TYPES: frozenset[str] = frozenset(
    {"decision", "bugfix", "feature", "refactor", "discovery", "change"}
)


def _format_full_observation_card(
    obs: ObservationRecord,
    ver: LineAnchorVerification | None,
) -> str:
    stale_banner = (
        "\n  WARNING: [STALE - CODE CHANGED] Anchored lines in this observation no longer match "
        + "current disk code. Do NOT trust literal values without calling `read_file`."
        if ver is not None and ver.state == "changed"
        else ""
    )
    anchor_suffix = format_line_anchor_suffix(ver)
    lines: list[str] = [
        f"### Observation #{obs.id} [{obs.type}] {obs.title}{anchor_suffix}{stale_banner}",
    ]
    if obs.subtitle:
        lines.append(f"- Subtitle: {obs.subtitle}")
    if obs.superseded_by is not None:
        lines.append(f"- Status: SUPERSEDED by #{obs.superseded_by} (prefer #{obs.superseded_by})")
    if obs.facts:
        lines.append("- Facts:")
        for fact in obs.facts:
            lines.append(f"  * {fact}")
    if obs.narrative:
        lines.append(f"- Narrative: {obs.narrative}")
    if obs.concepts:
        lines.append(f"- Concepts: {', '.join(obs.concepts)}")
    if obs.files_read:
        lines.append(f"- Files read: {', '.join(obs.files_read)}")
    if obs.files_modified:
        lines.append(f"- Files modified: {', '.join(obs.files_modified)}")
    return "\n".join(lines)


def build_memory_toolset(
    store: MemoryStorePort,
    *,
    project_key: str,
    workspace_root: str | Path | None = None,
    conversation_id: str = "default",
    role_name: str = "role",
) -> FunctionToolset[object]:
    """构建挂载于 Role Agent 的三步渐进记忆检索与显式保存工具集。"""
    ws_path = Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
    toolset: FunctionToolset[object] = FunctionToolset()

    @toolset.tool_plain
    async def memory_search(
        query: str,
        type_filter: str | None = None,
        concept_filter: str | None = None,
        file_path: str | None = None,
        limit: int = 15,
    ) -> str:
        """Smart memory recall step 1: search compact observation index by keywords/type/concept/file."""
        typed_filter: ObservationType | None = (
            cast(ObservationType, type_filter)
            if type_filter in _VALID_OBS_TYPES
            else None
        )
        clamped_limit = min(max(1, limit), 50)
        hits = await store.search_observations(
            project_key=project_key,
            query=query,
            type_filter=typed_filter,
            concept_filter=concept_filter,
            file_path=file_path,
            limit=clamped_limit,
        )
        if not hits:
            return f"No memory observations matched query={query!r}."
        verifications = verify_observations_on_disk(hits, ws_path)
        rows = [
            format_observation_timeline_row(
                obs,
                verification=verifications.get(obs.id),
                include_facts=False,
            )
            for obs in hits
        ]
        rows.append(
            "Next: call `memory_timeline(anchor_id=<id>)` for surrounding context, "
            + "or `get_observations(ids=[<id>, ...])` for full facts & narrative."
        )
        return "\n".join(rows)

    @toolset.tool_plain
    async def memory_timeline(
        anchor_id: int,
        depth_before: int = 3,
        depth_after: int = 3,
    ) -> str:
        """Smart memory recall step 2: inspect chronological context around a selected observation ID."""
        items = await store.get_timeline(
            project_key=project_key,
            anchor_id=anchor_id,
            depth_before=min(max(0, depth_before), 20),
            depth_after=min(max(0, depth_after), 20),
        )
        if not items:
            return f"Observation #{anchor_id} not found in project {project_key!r}."
        verifications = verify_observations_on_disk(items, ws_path)
        lines: list[str] = [f"Timeline around #{anchor_id}:"]
        for obs in items:
            marker = ">>> " if obs.id == anchor_id else "    "
            row = format_observation_timeline_row(
                obs,
                verification=verifications.get(obs.id),
                include_facts=False,
            )
            lines.append(f"{marker}{row}")
        lines.append("Next: call `get_observations(ids=[...])` for selected IDs you need details on.")
        return "\n".join(lines)

    @toolset.tool_plain
    async def get_observations(ids: list[int]) -> str:
        """Smart memory recall step 3: fetch full facts, narrative, files, and live line-anchor status for selected IDs."""
        bounded_ids = ids[:50]
        records = await store.get_observations_by_ids(project_key=project_key, ids=bounded_ids)
        if not records:
            return f"No observations found for IDs: {bounded_ids}"
        verifications = verify_observations_on_disk(records, ws_path)
        return "\n\n".join(
            _format_full_observation_card(obs, verifications.get(obs.id)) for obs in records
        )

    @toolset.tool_plain
    async def save_memory(
        title: str,
        facts: list[str],
        type: str = "decision",
        subtitle: str = "",
        narrative: str = "",
        concepts: Sequence[str] = ("pattern",),
        files_modified: Sequence[str] = (),
    ) -> str:
        """Persist a durable architectural decision, bugfix root cause, or project gotcha into memory."""
        obs_type: ObservationType = (
            cast(ObservationType, type) if type in _VALID_OBS_TYPES else "decision"
        )
        anchors: list[LineAnchor] = []
        file_hashes: dict[str, str] = {}
        norm_files: list[str] = []
        for raw_path in files_modified:
            norm = normalize_anchor_path(raw_path)
            if not norm:
                continue
            norm_files.append(norm)
            content = read_workspace_text(ws_path, norm)
            if content is not None:
                file_hashes[norm] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                anchors.extend(extract_line_anchors_from_content(norm, content))

        saved = await store.save_observation(
            CreateObservationInput(
                project_key=project_key,
                conversation_id=conversation_id,
                role_name=role_name,
                type=obs_type,
                title=title,
                subtitle=subtitle,
                facts=facts,
                narrative=narrative,
                concepts=tuple(concepts),
                files_modified=tuple(norm_files),
                line_anchors=tuple(anchors[:3]),
                file_hashes=file_hashes,
                provenance="user_explicit",
            )
        )
        action = "merged into existing" if saved.merged_into_existing else "stored as new"
        return f"Memory observation #{saved.record.id} ({action}): {saved.record.title}"

    return toolset
