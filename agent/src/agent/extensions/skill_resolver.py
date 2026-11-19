"""Skills 三层作用域解析、同级 SemVer 仲裁与 L1 目录索引构建（零 Core/DB/Permissions 依赖）。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path
from typing import cast, override

import yaml
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.artifact_scanning import ArtifactScanner
from pydantic_ai_harness.skills import SkillDefinition, Skills, SkippedSkill

from agent.context.instructions import (
    AGENT_CONFIG_DIR,
    SKILLS_SUBDIR,
    default_global_agent_dir,
    resolve_workspace_subdir,
)

DEFAULT_SKILL_VERSION = "0.0.0"
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / SKILLS_SUBDIR

_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    + r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    + r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class SkillScope(IntEnum):
    """Skill 作用域优先级（数值越小优先级越高）。"""

    WORKSPACE = 0
    GLOBAL = 1
    BUILTIN = 2


@dataclass(frozen=True)
class SemVer:
    """遵循 SemVer 2.0.0 优先级规则的语义版本号（忽略 build metadata）。"""

    major: int = 0
    minor: int = 0
    patch: int = 0
    prerelease: tuple[str, ...] = ()

    @override
    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{'.'.join(self.prerelease)}" if self.prerelease else base

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        lhs = (self.major, self.minor, self.patch)
        rhs = (other.major, other.minor, other.patch)
        if lhs != rhs:
            return lhs < rhs
        if self.prerelease == other.prerelease:
            return False
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        return _compare_prerelease(self.prerelease, other.prerelease) < 0

    def __le__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return other < self

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return other <= self


def _compare_prerelease(lhs: tuple[str, ...], rhs: tuple[str, ...]) -> int:
    for a, b in zip(lhs, rhs, strict=False):
        if a == b:
            continue
        a_num, b_num = a.isdigit(), b.isdigit()
        if a_num and b_num:
            return -1 if int(a) < int(b) else 1
        if a_num != b_num:
            return -1 if a_num else 1
        return -1 if a < b else 1
    if len(lhs) == len(rhs):
        return 0
    return -1 if len(lhs) < len(rhs) else 1


def parse_semver(raw: str | None) -> tuple[SemVer, bool]:
    """解析可选 SemVer 字符串；空或缺省返回 (0.0.0, True)，非法返回 (0.0.0, False)。"""
    if raw is None or not raw.strip():
        return SemVer(), True
    match = _SEMVER_RE.match(raw.strip())
    if match is None:
        return SemVer(), False
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    pre_group = match.group(4)
    if pre_group is None:
        return SemVer(major=major, minor=minor, patch=patch), True
    segments = tuple(pre_group.split("."))
    for seg in segments:
        if seg.isdigit() and len(seg) > 1 and seg.startswith("0"):
            return SemVer(), False
    return SemVer(major=major, minor=minor, patch=patch, prerelease=segments), True


@dataclass(frozen=True, kw_only=True)
class SkillSourceSpec:
    """单个 Skill 扫描目录配置。"""

    scope: SkillScope
    directory: Path
    scanner: ArtifactScanner | None = None


@dataclass(frozen=True, kw_only=True)
class ResolvedSkill:
    """已校验并附带作用域与版本元数据的 Skill 定义。"""

    definition: SkillDefinition
    scope: SkillScope
    version: SemVer
    version_raw: str
    source_dir: Path

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def description(self) -> str:
        return self.definition.description

    @property
    def when_to_use(self) -> str | None:
        return self.definition.when_to_use

    @property
    def directory(self) -> str:
        return self.definition.directory

    @property
    def path(self) -> str:
        return self.definition.path

    @property
    def model_invocable(self) -> bool:
        return self.definition.model_invocable


@dataclass(frozen=True, kw_only=True)
class SkillIndexEntry:
    """L1 轻量目录索引条目（仅含元数据，不含正文）。"""

    name: str
    description: str
    when_to_use: str | None
    scope: SkillScope
    version: str
    directory: str


@dataclass(frozen=True, kw_only=True)
class ResolvedSkillCatalog:
    """三层扫描与仲裁后的完整 Skill 清单及跳过告警记录。"""

    skills: tuple[ResolvedSkill, ...]
    skipped: tuple[SkippedSkill, ...]
    work_path: Path | None

    def l1_index(self) -> tuple[SkillIndexEntry, ...]:
        """返回对模型可见（model_invocable=True）的 L1 目录索引。"""
        return tuple(
            SkillIndexEntry(
                name=item.name,
                description=item.description,
                when_to_use=item.when_to_use,
                scope=item.scope,
                version=item.version_raw,
                directory=item.directory,
            )
            for item in self.skills
            if item.model_invocable
        )


def _normalize_skill_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).lower()


def _extract_skill_version(skill_md_path: Path) -> tuple[SemVer, str]:
    """仅读取 SKILL.md 头部 YAML frontmatter，解析 version 或 metadata.version。"""
    try:
        with skill_md_path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
            if first.rstrip("\r\n") != "---":
                return SemVer(), DEFAULT_SKILL_VERSION
            fm_lines: list[str] = []
            closed = False
            for line in fh:
                if line.rstrip("\r\n") == "---":
                    closed = True
                    break
                fm_lines.append(line.rstrip("\r\n"))
            if not closed:
                return SemVer(), DEFAULT_SKILL_VERSION
    except (OSError, UnicodeDecodeError):
        return SemVer(), DEFAULT_SKILL_VERSION

    try:
        parsed = cast(object, yaml.load("\n".join(fm_lines), Loader=yaml.BaseLoader))
    except yaml.YAMLError:
        return SemVer(), DEFAULT_SKILL_VERSION
    if not isinstance(parsed, dict):
        return SemVer(), DEFAULT_SKILL_VERSION

    mapping = cast(dict[object, object], parsed)
    raw_ver = mapping.get("version")
    if raw_ver is None and isinstance(mapping.get("metadata"), dict):
        meta = cast(dict[object, object], mapping["metadata"])
        raw_ver = meta.get("version")

    if isinstance(raw_ver, str) and raw_ver.strip():
        ver, ok = parse_semver(raw_ver)
        if ok:
            return ver, str(ver)
    return SemVer(), DEFAULT_SKILL_VERSION


def arbitrate_skill_candidates(
    candidates: Sequence[ResolvedSkill],
    *,
    blocked_names: Collection[str] = (),
    blocked_scopes: Mapping[str, SkillScope] | None = None,
) -> tuple[tuple[ResolvedSkill, ...], tuple[SkippedSkill, ...]]:
    """纯函数仲裁：跨作用域 (WORKSPACE > GLOBAL > BUILTIN) + 同级 SemVer 高版本胜出。"""
    blocked = {_normalize_skill_name(name) for name in blocked_names}
    scope_blocks = (
        {_normalize_skill_name(name): scope for name, scope in blocked_scopes.items()}
        if blocked_scopes
        else {}
    )
    winners: dict[str, ResolvedSkill] = {}
    winner_realpaths: dict[str, Path] = {}
    skipped: list[SkippedSkill] = []

    for cand in candidates:
        norm_name = _normalize_skill_name(cand.name)
        if norm_name in blocked:
            continue
        blocked_scope = scope_blocks.get(norm_name)
        if blocked_scope is not None and cand.scope >= blocked_scope:
            continue
        cand_realpath = Path(cand.path).resolve()
        prev = winners.get(cand.name)
        if prev is None:
            winners[cand.name] = cand
            winner_realpaths[cand.name] = cand_realpath
            continue

        prev_realpath = winner_realpaths[cand.name]
        if cand_realpath == prev_realpath:
            if cand.scope < prev.scope:
                winners[cand.name] = cand
            continue

        if cand.scope != prev.scope:
            if cand.scope < prev.scope:
                winners[cand.name] = cand
                winner_realpaths[cand.name] = cand_realpath
                loser = prev
                reason_msg = (
                    f"Skipping {loser.path}: skill name {cand.name!r} overridden by higher-precedence "
                    f"scope {cand.scope.name.lower()} at {cand.path}."
                )
            else:
                loser = cand
                reason_msg = (
                    f"Skipping {loser.path}: skill name {cand.name!r} is already provided by "
                    f"higher-precedence scope {prev.scope.name.lower()} at {prev.path}."
                )
            skipped.append(SkippedSkill(path=loser.path, reason="duplicate_name", message=reason_msg))
            continue

        if cand.version > prev.version:
            winners[cand.name] = cand
            winner_realpaths[cand.name] = cand_realpath
            skipped.append(
                SkippedSkill(
                    path=prev.path,
                    reason="duplicate_name",
                    message=(
                        f"Skipping {prev.path}: skill name {cand.name!r} (version {prev.version_raw}) "
                        f"overridden by higher version {cand.version_raw} at {cand.path}."
                    ),
                )
            )
        else:
            skipped.append(
                SkippedSkill(
                    path=cand.path,
                    reason="duplicate_name",
                    message=(
                        f"Skipping {cand.path}: skill name {cand.name!r} (version {cand.version_raw}) "
                        f"is already taken by {prev.path} (version {prev.version_raw})."
                    ),
                )
            )

    ordered = tuple(winners[name] for name in sorted(winners))
    return ordered, tuple(skipped)


class SkillCatalogResolver:
    """按三层作用域边界加载并仲裁 Skill 列表（WORKSAPCE > GLOBAL > BUILTIN）。"""

    def __init__(
        self,
        *,
        workspace_subdirs: Sequence[str] = (f"{AGENT_CONFIG_DIR}/{SKILLS_SUBDIR}",),
        global_dirs: Sequence[str | Path] | None = None,
        builtin_dirs: Sequence[str | Path] | None = None,
        include: Collection[str] | None = None,
        exclude: Collection[str] | None = None,
        scanner: ArtifactScanner | None = None,
        scan_builtin: bool = False,
    ) -> None:
        if include is not None and exclude is not None:
            raise ValueError("include and exclude cannot be used together.")
        self._workspace_subdirs: tuple[str, ...] = tuple(workspace_subdirs)
        self._global_dirs: tuple[Path, ...] = (
            (default_global_agent_dir() / SKILLS_SUBDIR,)
            if global_dirs is None
            else tuple(Path(p).expanduser().resolve() for p in global_dirs)
        )
        self._builtin_dirs: tuple[Path, ...] = (
            (BUILTIN_SKILLS_DIR,)
            if builtin_dirs is None
            else tuple(Path(p).expanduser().resolve() for p in builtin_dirs)
        )
        self._include: tuple[str, ...] | None = tuple(include) if include is not None else None
        self._exclude: tuple[str, ...] | None = tuple(exclude) if exclude is not None else None
        self._scanner: ArtifactScanner | None = scanner
        self._scan_builtin: bool = scan_builtin

    def build_sources(self, work_path: str | Path | None = None) -> tuple[SkillSourceSpec, ...]:
        """按工作区 > 全局 > 内置顺序生成去重后的扫描目录配置，绝不向上继承父目录。"""
        sources: list[SkillSourceSpec] = []
        seen_dirs: set[Path] = set()

        def _append(scope: SkillScope, directory: Path, scanner: ArtifactScanner | None) -> None:
            resolved = directory.resolve()
            if resolved in seen_dirs:
                return
            seen_dirs.add(resolved)
            sources.append(SkillSourceSpec(scope=scope, directory=resolved, scanner=scanner))

        norm_ws: Path | None = None
        if work_path is not None:
            for rel in self._workspace_subdirs:
                ws_dir = resolve_workspace_subdir(work_path, rel)
                if norm_ws is None:
                    norm_ws = ws_dir
                _append(SkillScope.WORKSPACE, ws_dir, self._scanner)

        for g_dir in self._global_dirs:
            _append(SkillScope.GLOBAL, g_dir, self._scanner)

        builtin_scanner = self._scanner if self._scan_builtin else None
        for b_dir in self._builtin_dirs:
            _append(SkillScope.BUILTIN, b_dir, builtin_scanner)

        return tuple(sources)

    def _build_loader(self, directory_name: str, scanner: ArtifactScanner | None) -> Skills[object]:
        if self._include is not None:
            return Skills(
                directory_name,
                include=self._include,
                missing_directories="skip",
                duplicate_names="keep_first",
                unknown_names="skip",
                scanner=scanner,
            )
        return Skills(
            directory_name,
            exclude=self._exclude,
            missing_directories="skip",
            duplicate_names="keep_first",
            unknown_names="skip",
            scanner=scanner,
        )

    async def resolve(self, work_path: str | Path | None = None) -> ResolvedSkillCatalog:
        """扫描有效目录并完成跨作用域去重与同级 SemVer 仲裁。"""
        norm_ws = Path(work_path).expanduser().resolve() if work_path is not None else None
        sources = self.build_sources(work_path=norm_ws)

        candidates: list[ResolvedSkill] = []
        skipped: list[SkippedSkill] = []
        blocked_scopes: dict[str, SkillScope] = {}

        for source in sources:
            if not source.directory.exists() or source.directory.parent == source.directory:
                continue
            loader = self._build_loader(source.directory.name, source.scanner)
            backend = LocalWorkspaceBackend(source.directory.parent)
            catalog = await loader.load(backend)
            for item in catalog.skipped:
                skipped.append(item)
                if item.reason == "flagged":
                    flagged_name = _normalize_skill_name(Path(item.path).parent.name)
                    prev_scope = blocked_scopes.get(flagged_name)
                    if prev_scope is None or source.scope < prev_scope:
                        blocked_scopes[flagged_name] = source.scope
            for skill_def in catalog.skills:
                ver, ver_raw = _extract_skill_version(Path(skill_def.path))
                unified_def = replace(skill_def, in_run_workspace=True)
                candidates.append(
                    ResolvedSkill(
                        definition=unified_def,
                        scope=source.scope,
                        version=ver,
                        version_raw=ver_raw,
                        source_dir=source.directory,
                    )
                )

        winners, dup_skipped = arbitrate_skill_candidates(
            candidates,
            blocked_scopes=blocked_scopes,
        )
        skipped.extend(dup_skipped)
        return ResolvedSkillCatalog(
            skills=winners,
            skipped=tuple(skipped),
            work_path=norm_ws,
        )
