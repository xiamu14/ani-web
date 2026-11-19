"""Hook 优先级约定与稳定排序器（对齐 Pydantic AI CombinedCapability 装配顺序）。"""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum
from typing import TypeVar

from pydantic_ai.capabilities import AbstractCapability

DepsT = TypeVar("DepsT")

DEFAULT_HOOK_PRIORITY = 50


class HookPriority(IntEnum):
    """能力/Hook 执行优先级（数值越小越先进入、越外层包裹）。"""

    PERMISSION = 0
    POLICY = 10
    TOOL_QUEUE = 20
    OBSERVABILITY = 30
    DEFAULT = DEFAULT_HOOK_PRIORITY
    PERSISTENCE = 90


def get_capability_priority(capability: AbstractCapability[DepsT]) -> int:
    """读取 Capability 的显式 hook_priority，未声明时按已知内置类型推导默认优先级。"""
    explicit = getattr(capability, "hook_priority", None)
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    if type(capability).__name__ == "StepPersistence":
        return int(HookPriority.PERSISTENCE)
    return DEFAULT_HOOK_PRIORITY


def sort_capabilities(
    capabilities: Sequence[AbstractCapability[DepsT]],
) -> list[AbstractCapability[DepsT]]:
    """按 HookPriority 稳定排序（同优先级保留原有传入顺序）。"""
    return sorted(capabilities, key=get_capability_priority)
