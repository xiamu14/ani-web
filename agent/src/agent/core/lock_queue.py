"""工作区读写锁队列与依赖状态管理（WorkspaceLockManager 内部协作模块）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from agent.db import paths_overlap
from agent.tools import LockMode, WorkspaceLockStatus


def as_lock_mode(value: str) -> LockMode:
    """将数据库持久化锁模式文字列安全收窄为 LockMode。"""
    if value == "exclusive":
        return "exclusive"
    if value == "shared":
        return "shared"
    if value == "none":
        return "none"
    return "exclusive"


@dataclass(frozen=True, kw_only=True)
class QueuedTask:
    """等待锁或前置依赖的排队任务项。"""

    main_conversation_id: str
    task_id: str
    norm_path: str | None
    lock_mode: LockMode
    depends_on: tuple[str, ...] = ()


class LockQueueState:
    """维护工作区读写锁、防写者饥饿 FIFO 队列及上游失败/中断阻塞集合。"""

    def __init__(self) -> None:
        self._active_exclusive: dict[str, tuple[str, str]] = {}
        self._active_shared: dict[str, set[tuple[str, str]]] = {}
        self._pending_queue: list[QueuedTask] = []
        self._unfinished: set[tuple[str, str]] = set()
        self._blocked_upstreams: set[tuple[str, str]] = set()

    @staticmethod
    def normalize_path(work_path: str | Path | None) -> str | None:
        return str(Path(work_path).expanduser().resolve()) if work_path is not None else None

    def mark_unfinished(self, main_conversation_id: str, task_id: str) -> None:
        key = (main_conversation_id, task_id)
        self._blocked_upstreams.discard(key)
        self._unfinished.add(key)

    def mark_settled(self, main_conversation_id: str, task_id: str, *, blocked: bool) -> None:
        key = (main_conversation_id, task_id)
        self._unfinished.discard(key)
        if blocked:
            self._blocked_upstreams.add(key)
        else:
            self._blocked_upstreams.discard(key)

    def remove_dependency(self, main_conversation_id: str, cancelled_task_id: str) -> None:
        """上游任务被显式取消后，解除所有内存排队任务对该 ID 的依赖。"""
        target = cancelled_task_id.strip().lstrip("@")
        self._unfinished.discard((main_conversation_id, target))
        self._blocked_upstreams.discard((main_conversation_id, target))
        updated: list[QueuedTask] = []
        for item in self._pending_queue:
            if item.main_conversation_id == main_conversation_id and target in item.depends_on:
                new_deps = tuple(d for d in item.depends_on if d != target)
                updated.append(replace(item, depends_on=new_deps))
            else:
                updated.append(item)
        self._pending_queue = updated

    def drop_queued(self, main_conversation_id: str, task_id: str) -> bool:
        before = len(self._pending_queue)
        self._pending_queue = [
            q
            for q in self._pending_queue
            if not (q.main_conversation_id == main_conversation_id and q.task_id == task_id)
        ]
        return len(self._pending_queue) < before

    def is_queued(self, main_conversation_id: str, task_id: str) -> bool:
        return any(
            q.main_conversation_id == main_conversation_id and q.task_id == task_id
            for q in self._pending_queue
        )

    def _has_blocking_deps(self, main_conversation_id: str, depends_on: tuple[str, ...]) -> bool:
        for dep_id in depends_on:
            key = (main_conversation_id, dep_id)
            if key in self._unfinished or key in self._blocked_upstreams:
                return True
        return False

    def can_acquire(
        self,
        *,
        main_conversation_id: str,
        task_id: str | None = None,
        norm_path: str | None,
        lock_mode: LockMode,
        depends_on: tuple[str, ...],
        ahead: Sequence[QueuedTask] | None = None,
    ) -> bool:
        if self._has_blocking_deps(main_conversation_id, depends_on):
            return False
        if norm_path is None or lock_mode == "none":
            return True

        queue_ahead = self._pending_queue if ahead is None else ahead
        effective_ahead = [
            q
            for q in queue_ahead
            if not self._has_blocking_deps(q.main_conversation_id, q.depends_on)
            and not (
                task_id is not None
                and q.main_conversation_id == main_conversation_id
                and task_id in q.depends_on
            )
        ]
        if any(paths_overlap(norm_path, p) for p in self._active_exclusive):
            return False

        if lock_mode == "exclusive":
            has_active_shared = any(
                holders and paths_overlap(norm_path, p)
                for p, holders in self._active_shared.items()
            )
            if has_active_shared:
                return False
            return not any(
                q.norm_path is not None
                and q.lock_mode != "none"
                and paths_overlap(norm_path, q.norm_path)
                for q in effective_ahead
            )

        return not any(
            q.norm_path is not None
            and q.lock_mode == "exclusive"
            and paths_overlap(norm_path, q.norm_path)
            for q in effective_ahead
        )

    def acquire_lock(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        norm_path: str | None,
        lock_mode: LockMode,
    ) -> None:
        if norm_path is None or lock_mode == "none":
            return
        key = (main_conversation_id, task_id)
        if lock_mode == "exclusive":
            self._active_exclusive[norm_path] = key
        else:
            self._active_shared.setdefault(norm_path, set()).add(key)

    def release_lock(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        norm_path: str | None,
    ) -> None:
        if norm_path is None:
            return
        key = (main_conversation_id, task_id)
        if self._active_exclusive.get(norm_path) == key:
            _ = self._active_exclusive.pop(norm_path, None)
        holders = self._active_shared.get(norm_path)
        if holders is not None:
            holders.discard(key)
            if not holders:
                _ = self._active_shared.pop(norm_path, None)

    def enqueue(self, item: QueuedTask) -> int:
        self._pending_queue.append(item)
        return sum(
            1
            for q in self._pending_queue
            if item.norm_path is None
            or q.norm_path is None
            or paths_overlap(item.norm_path, q.norm_path)
        )

    def pop_ready_tasks(self) -> list[QueuedTask]:
        started: list[QueuedTask] = []
        remaining: list[QueuedTask] = []
        for item in self._pending_queue:
            if self.can_acquire(
                main_conversation_id=item.main_conversation_id,
                task_id=item.task_id,
                norm_path=item.norm_path,
                lock_mode=item.lock_mode,
                depends_on=item.depends_on,
                ahead=remaining,
            ):
                self.acquire_lock(
                    main_conversation_id=item.main_conversation_id,
                    task_id=item.task_id,
                    norm_path=item.norm_path,
                    lock_mode=item.lock_mode,
                )
                started.append(item)
            else:
                remaining.append(item)
        self._pending_queue = remaining
        return started

    def inspect(self, work_path: str | None = None) -> list[WorkspaceLockStatus]:
        target = self.normalize_path(work_path)
        all_paths = sorted(
            {
                *self._active_exclusive.keys(),
                *self._active_shared.keys(),
                *(q.norm_path for q in self._pending_queue if q.norm_path is not None),
            }
        )
        snapshots: list[WorkspaceLockStatus] = []
        for path in all_paths:
            if target is not None and not paths_overlap(target, path):
                continue
            excl = self._active_exclusive.get(path)
            shared = sorted(f"@{tid}" for _, tid in self._active_shared.get(path, set()))
            queued = tuple(
                f"@{q.task_id}({q.lock_mode})"
                for q in self._pending_queue
                if q.norm_path is not None and paths_overlap(path, q.norm_path)
            )
            snapshots.append(
                WorkspaceLockStatus(
                    work_path=path,
                    exclusive_holder=f"@{excl[1]}" if excl is not None else None,
                    shared_holders=tuple(shared),
                    queued_tasks=queued,
                )
            )
        return snapshots
