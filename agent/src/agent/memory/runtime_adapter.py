"""运行时记忆装配适配器: 负责 Role 启动 L0+L1 注入 (full_fork 自动跳过)、Handoff 状态恢复与任务完成归档。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.toolsets import AbstractToolset

from agent.attachments import AttachmentRegistry, build_attachment_toolset
from agent.memory.budget import render_l0_l1_context
from agent.memory.extractor import TrajectoryMemoryExtractor
from agent.memory.ports import MemoryStorePort
from agent.memory.rank import pool_size
from agent.memory.toolset import build_memory_toolset
from agent.memory.workspace_io import verify_observations_on_disk


@dataclass(frozen=True, kw_only=True)
class RoleMemoryBundle:
    """单次 Role Worker 运行装配的记忆指令、Hook 与 Toolset 组合包。"""

    extra_instructions: str = ""
    capabilities: tuple[AbstractCapability[object], ...] = ()
    toolsets: tuple[AbstractToolset[object], ...] = ()


def derive_project_key(work_path: str | Path | None) -> str:
    """将工作区路径规范化为稳定的 project_key（未绑定工作区时归入 global）。"""
    if work_path is None or not str(work_path).strip():
        return "global"
    return str(Path(work_path).expanduser().resolve())


async def build_l0_l1_memory_block(
    store: MemoryStorePort,
    *,
    work_path: str | Path | None,
    branch_mode: str = "root_only",
    observation_limit: int = 30,
    reinforcement_alpha: float = 1.0,
) -> str:
    """构建会话或 Role 启动时的 L0+L1 记忆块；当 branch_mode == 'full_fork' 时跳过以防重复注入。"""
    if branch_mode == "full_fork":
        return ""
    project_key = derive_project_key(work_path)
    ws_root = Path(work_path).expanduser().resolve() if work_path is not None else None
    fetch_count = pool_size(observation_limit, reinforcement_alpha)

    observations, summaries, work_states = await asyncio.gather(
        store.list_recent_observations(
            project_key=project_key,
            limit=fetch_count,
            include_superseded=False,
        ),
        store.list_recent_summaries(project_key=project_key, limit=1),
        store.list_work_states(project_key=project_key),
    )
    verifications = verify_observations_on_disk(observations, ws_root)

    return render_l0_l1_context(
        observations=observations,
        summaries=summaries,
        work_states=work_states,
        observation_limit=observation_limit,
        reinforcement_alpha=reinforcement_alpha,
        anchor_verifications=verifications,
    )


async def build_l0_compaction_recovery_block(
    store: MemoryStorePort,
    *,
    work_path: str | Path | None,
) -> str:
    """在 Handoff / Compaction 后恢复 L0 状态（Open WorkState + Latest SessionSummary）。"""
    project_key = derive_project_key(work_path)
    summaries, work_states = await asyncio.gather(
        store.list_recent_summaries(project_key=project_key, limit=1),
        store.list_work_states(project_key=project_key),
    )
    return render_l0_l1_context(
        observations=(),
        summaries=summaries,
        work_states=work_states,
        observation_limit=1,
    )


async def prepare_role_memory_bundle(
    store: MemoryStorePort | None,
    *,
    work_path: str | Path | None,
    conversation_id: str,
    role_name: str,
    branch_mode: str = "root_only",
    attachment_registry: AttachmentRegistry | None = None,
) -> RoleMemoryBundle:
    """为 Role Worker 构建 L0+L1 指令块、L2 FileMemoryContextHook、L3 MemoryToolset 与 AttachmentToolset。"""
    extra_toolsets: list[AbstractToolset[object]] = []
    if attachment_registry is not None:
        extra_toolsets.append(
            build_attachment_toolset(attachment_registry, workspace_root=work_path)
        )
    if store is None:
        return RoleMemoryBundle(toolsets=tuple(extra_toolsets))
    from agent.hooks.memory_hook import FileMemoryContextHook

    project_key = derive_project_key(work_path)
    l0_l1 = await build_l0_l1_memory_block(
        store,
        work_path=work_path,
        branch_mode=branch_mode,
    )
    file_hook = FileMemoryContextHook(
        store=store,
        project_key=project_key,
        workspace_root=work_path,
        conversation_id=conversation_id,
        role_name=role_name,
    )
    mem_toolset = build_memory_toolset(
        store,
        project_key=project_key,
        workspace_root=work_path,
        conversation_id=conversation_id,
        role_name=role_name,
    )
    return RoleMemoryBundle(
        extra_instructions=l0_l1,
        capabilities=(file_hook,),
        toolsets=(mem_toolset, *extra_toolsets),
    )


async def archive_completed_role_task(
    store: MemoryStorePort | None,
    *,
    work_path: str | Path | None,
    conversation_id: str,
    task_id: str,
    role_name: str,
    mandate: str,
    output: str,
    messages: Sequence[ModelMessage],
    discovery_tokens: int = 0,
) -> None:
    """在 Role 任务成功完成时提炼并归档 Observation 与 SessionSummary。"""
    if store is None:
        return
    project_key = derive_project_key(work_path)
    extractor = TrajectoryMemoryExtractor(store)
    _ = await extractor.extract_and_store(
        project_key=project_key,
        conversation_id=conversation_id,
        task_id=task_id,
        role_name=role_name,
        mandate=mandate,
        output=output,
        messages=messages,
        workspace_root=work_path,
        discovery_tokens=discovery_tokens,
    )
