"""同进程分层记忆子系统契约层: 数据模型与解耦端口协议（零 IO / 零 DB / 零 Runtime 依赖）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

ObservationType = Literal["decision", "bugfix", "feature", "refactor", "discovery", "change"]
ObservationConcept = Literal[
    "how-it-works",
    "why-it-exists",
    "what-changed",
    "problem-solution",
    "gotcha",
    "pattern",
    "trade-off",
]
MemoryProvenance = Literal["verified_by_tool", "user_explicit", "agent_inferred"]
WorkStateStatus = Literal["open", "in_progress", "blocked", "done", "cancelled"]
LineAnchorState = Literal["found", "partly-changed", "changed"]
DedupTier = Literal["exact", "candidate", "none"]
DedupMethod = Literal["exact", "idf_cosine", "none"]


def _empty_str_dict() -> dict[str, str]:
    return {}


def _empty_nullable_str_dict() -> dict[str, str | None]:
    return {}


@dataclass(frozen=True, kw_only=True)
class LineAnchor:
    """从真实 Read/Edit 工具报文提取的文件内唯一代码行物理锚点。"""

    path: str
    line: int
    text: str


@dataclass(frozen=True, kw_only=True)
class LineAnchorVerification:
    """读取时对照当前磁盘文件实时重算的行锚点状态。"""

    state: LineAnchorState
    lines: tuple[int, ...]
    missing: int


@dataclass(frozen=True, kw_only=True)
class ObservationRecord:
    """持久化后的原子记忆观察（Observation）。"""

    id: int
    project_key: str
    conversation_id: str
    role_name: str
    type: ObservationType
    title: str
    subtitle: str = ""
    facts: tuple[str, ...] = ()
    narrative: str = ""
    concepts: tuple[str, ...] = ()
    files_read: tuple[str, ...] = ()
    files_modified: tuple[str, ...] = ()
    line_anchors: tuple[LineAnchor, ...] = ()
    file_hashes: Mapping[str, str] = field(default_factory=_empty_str_dict)
    provenance: MemoryProvenance = "verified_by_tool"
    discovery_tokens: int = 0
    reinforcement_dates: tuple[str, ...] = ()
    superseded_by: int | None = None
    created_at_epoch: int = 0


@dataclass(frozen=True, kw_only=True)
class CreateObservationInput:
    """写入新 Observation 的输入载荷。"""

    project_key: str
    conversation_id: str
    role_name: str
    type: ObservationType
    title: str
    subtitle: str = ""
    facts: Sequence[str] = ()
    narrative: str = ""
    concepts: Sequence[str] = ()
    files_read: Sequence[str] = ()
    files_modified: Sequence[str] = ()
    line_anchors: Sequence[LineAnchor] = ()
    file_hashes: Mapping[str, str] = field(default_factory=_empty_str_dict)
    provenance: MemoryProvenance = "verified_by_tool"
    discovery_tokens: int = 0
    created_at_epoch: int | None = None


@dataclass(frozen=True, kw_only=True)
class SessionSummaryRecord:
    """任务/会话级阶段总结记录（SessionSummary）。"""

    id: int
    project_key: str
    conversation_id: str
    task_id: str
    request: str = ""
    investigated: str = ""
    learned: str = ""
    completed: str = ""
    next_steps: str = ""
    notes: str = ""
    files_read: tuple[str, ...] = ()
    files_edited: tuple[str, ...] = ()
    discovery_tokens: int = 0
    created_at_epoch: int = 0


@dataclass(frozen=True, kw_only=True)
class CreateSessionSummaryInput:
    """写入新 SessionSummary 的输入载荷。"""

    project_key: str
    conversation_id: str
    task_id: str
    request: str = ""
    investigated: str = ""
    learned: str = ""
    completed: str = ""
    next_steps: str = ""
    notes: str = ""
    files_read: Sequence[str] = ()
    files_edited: Sequence[str] = ()
    discovery_tokens: int = 0
    created_at_epoch: int | None = None


@dataclass(frozen=True, kw_only=True)
class WorkStateEntryRecord:
    """开放工作状态追加账本行（WorkStateEntry）。"""

    id: int
    project_key: str
    list_name: str
    task_name: str
    fields: Mapping[str, str | None] = field(default_factory=_empty_nullable_str_dict)
    status: WorkStateStatus = "open"
    created_at_epoch: int = 0


@dataclass(frozen=True, kw_only=True)
class FoldedWorkStateTask:
    """按写入顺序折叠后的单个 WorkState 任务快照。"""

    list_name: str
    task_name: str
    status: WorkStateStatus
    fields: Mapping[str, str]
    updated_at_epoch: int


@dataclass(frozen=True, kw_only=True)
class SaveObservationResult:
    """Observation 入库结果（指示是新增、Tier-0 强化合并还是覆盖旧记录）。"""

    record: ObservationRecord
    merged_into_existing: bool
    superseded_ids: tuple[int, ...] = ()


class MemoryStorePort(Protocol):
    """同进程异步记忆仓储端口协议。"""

    async def save_observation(self, item: CreateObservationInput) -> SaveObservationResult: ...

    async def save_summary(self, item: CreateSessionSummaryInput) -> SessionSummaryRecord: ...

    async def append_work_state(
        self,
        *,
        project_key: str,
        list_name: str,
        task_name: str,
        status: WorkStateStatus = "open",
        fields: Mapping[str, str | None] | None = None,
        created_at_epoch: int | None = None,
    ) -> WorkStateEntryRecord: ...

    async def search_observations(
        self,
        *,
        project_key: str,
        query: str,
        type_filter: ObservationType | None = None,
        concept_filter: str | None = None,
        file_path: str | None = None,
        include_superseded: bool = False,
        limit: int = 15,
    ) -> list[ObservationRecord]: ...

    async def get_timeline(
        self,
        *,
        project_key: str,
        anchor_id: int,
        depth_before: int = 3,
        depth_after: int = 3,
    ) -> list[ObservationRecord]: ...

    async def get_observations_by_ids(
        self,
        *,
        project_key: str,
        ids: Sequence[int],
    ) -> list[ObservationRecord]: ...

    async def list_recent_observations(
        self,
        *,
        project_key: str,
        limit: int = 50,
        include_superseded: bool = False,
    ) -> list[ObservationRecord]: ...

    async def list_observations_for_file(
        self,
        *,
        project_key: str,
        file_path: str,
        limit: int = 15,
        include_superseded: bool = False,
    ) -> list[ObservationRecord]: ...

    async def list_recent_summaries(
        self,
        *,
        project_key: str,
        limit: int = 5,
    ) -> list[SessionSummaryRecord]: ...

    async def list_work_states(
        self,
        *,
        project_key: str,
    ) -> list[WorkStateEntryRecord]: ...

    async def mark_superseded(
        self,
        *,
        project_key: str,
        observation_ids: Sequence[int],
        superseded_by: int,
    ) -> None: ...
