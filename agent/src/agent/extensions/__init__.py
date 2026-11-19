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
from agent.extensions.tool_search import (
    DeferToolsCapability,
    ToolDisclosurePolicy,
    bilingual_tool_search_fn,
    build_tool_search_capabilities,
)

__all__ = [
    "BUILTIN_SKILLS_DIR",
    "DEFAULT_SKILL_VERSION",
    "DeferToolsCapability",
    "ProgressiveSkillsCapability",
    "ResolvedSkill",
    "ResolvedSkillCatalog",
    "SemVer",
    "SkillCatalogResolver",
    "SkillIndexEntry",
    "SkillScope",
    "SkillSourceSpec",
    "ToolDisclosurePolicy",
    "arbitrate_skill_candidates",
    "bilingual_tool_search_fn",
    "build_skills_capability",
    "build_tool_search_capabilities",
    "parse_semver",
]
