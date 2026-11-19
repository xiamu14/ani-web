"""扩展能力包: 对外暴露渐进披露 Skills 解析器与 Capability 适配器。"""

from agent.extensions.skill_resolver import (
    BUILTIN_SKILLS_DIR,
    DEFAULT_SKILL_VERSION,
    ResolvedSkill,
    ResolvedSkillCatalog,
    SemVer,
    SkillCatalogResolver,
    SkillIndexEntry,
    SkillScope,
    SkillSourceSpec,
    arbitrate_skill_candidates,
    parse_semver,
)
from agent.extensions.skills import ProgressiveSkillsCapability, build_skills_capability

__all__ = [
    "BUILTIN_SKILLS_DIR",
    "DEFAULT_SKILL_VERSION",
    "ProgressiveSkillsCapability",
    "ResolvedSkill",
    "ResolvedSkillCatalog",
    "SemVer",
    "SkillCatalogResolver",
    "SkillIndexEntry",
    "SkillScope",
    "SkillSourceSpec",
    "arbitrate_skill_candidates",
    "build_skills_capability",
    "parse_semver",
]
