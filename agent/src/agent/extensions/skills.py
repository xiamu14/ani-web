"""Role Agent 渐进披露 Skills Capability 适配器（三层平等渲染 + 动态刷新）。"""

from __future__ import annotations

import warnings
from collections.abc import Collection, Sequence
from dataclasses import replace
from pathlib import Path
from typing import override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, Capability, CombinedCapability
from pydantic_ai_harness.artifact_scanning import ArtifactScanner

from agent.context.instructions import AGENT_CONFIG_DIR, SKILLS_SUBDIR
from agent.extensions.skill_resolver import (
    ResolvedSkill,
    ResolvedSkillCatalog,
    SkillCatalogResolver,
)
from agent.hooks.priority import HookPriority

_MAX_DESCRIPTION_LENGTH = 1024


def _format_catalog_description(skill: ResolvedSkill) -> str:
    text = (
        skill.description
        if skill.when_to_use is None
        else f"{skill.description} {skill.when_to_use}"
    )
    return text.replace("\n", "\n  ")


def _emit_catalog_warnings(catalog: ResolvedSkillCatalog) -> None:
    for skipped in catalog.skipped:
        warnings.warn(skipped.message, UserWarning, stacklevel=3)

    overlong = [
        f"{item.name} ({len(item.description):,} characters)"
        for item in catalog.skills
        if len(item.description) > _MAX_DESCRIPTION_LENGTH
    ]
    if overlong:
        warnings.warn(
            f"Agent Skill descriptions exceed the {_MAX_DESCRIPTION_LENGTH:,}-character limit: "
            + "; ".join(overlong),
            UserWarning,
            stacklevel=3,
        )

    ignored = [
        f"{item.name}: {', '.join(item.definition.ignored_behavioral_fields)}"
        for item in catalog.skills
        if item.definition.ignored_behavioral_fields
    ]
    if ignored:
        warnings.warn(
            "Ignoring unsupported Agent Skill behavioral frontmatter fields: " + "; ".join(ignored),
            UserWarning,
            stacklevel=3,
        )


class ProgressiveSkillsCapability(AbstractCapability[object]):
    """为 Role Agent 提供基于 ctx.workspace 动态刷新的 L1/L2 Skills 渐进披露能力。"""

    hook_priority: int = int(HookPriority.DEFAULT)

    def __init__(
        self,
        *,
        resolver: SkillCatalogResolver | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._resolver: SkillCatalogResolver = resolver or SkillCatalogResolver()
        self._workspace_root: str | Path | None = workspace_root
        self._last_catalog: ResolvedSkillCatalog | None = None

    @property
    def last_catalog(self) -> ResolvedSkillCatalog | None:
        """返回最近一次 `for_run` 解析产出的 Skill 目录快照。"""
        return self._last_catalog

    def _resolve_work_path(self, ctx: RunContext[object]) -> Path | None:
        if self._workspace_root is not None:
            return Path(self._workspace_root).expanduser().resolve()
        ref = ctx.workspace.ref if ctx.workspace.attached else None
        if ref is not None and ref.provider == "local":
            return Path(ref.id).expanduser().resolve()
        return None

    @override
    async def for_run(self, ctx: RunContext[object]) -> AbstractCapability[object]:
        work_path = self._resolve_work_path(ctx)
        catalog = await self._resolver.resolve(work_path=work_path)
        self._last_catalog = catalog
        _emit_catalog_warnings(catalog)

        ref = ctx.workspace.ref if ctx.workspace.attached else None
        is_local = ref is None or ref.provider == "local"
        offered: list[AbstractCapability[object]] = []
        for skill in catalog.skills:
            if not skill.model_invocable:
                continue
            effective = replace(skill.definition, in_run_workspace=is_local)
            offered.append(
                Capability[object](
                    id=effective.name,
                    description=_format_catalog_description(skill),
                    instructions=effective.render(),
                    defer_loading=True,
                )
            )
        return CombinedCapability[object](offered) if offered else self


def build_skills_capability(
    *,
    workspace_subdirs: Sequence[str] = (f"{AGENT_CONFIG_DIR}/{SKILLS_SUBDIR}",),
    global_dirs: Sequence[str | Path] | None = None,
    builtin_dirs: Sequence[str | Path] | None = None,
    include: Collection[str] | None = None,
    exclude: Collection[str] | None = None,
    scanner: ArtifactScanner | None = None,
    scan_builtin: bool = False,
    workspace_root: str | Path | None = None,
) -> ProgressiveSkillsCapability:
    """构建默认三层作用域渐进披露 Skills Capability 实例。"""
    resolver = SkillCatalogResolver(
        workspace_subdirs=workspace_subdirs,
        global_dirs=global_dirs,
        builtin_dirs=builtin_dirs,
        include=include,
        exclude=exclude,
        scanner=scanner,
        scan_builtin=scan_builtin,
    )
    return ProgressiveSkillsCapability(resolver=resolver, workspace_root=workspace_root)
