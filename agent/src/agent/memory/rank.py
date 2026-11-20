"""ACT-R 基础激活度排序层: 结合新鲜度幂律衰减与跨天强化日期排序（保留最新 1/4 头部窗口，零 IO）。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

ACT_R_DECAY = 0.5
SECONDS_PER_DAY = 86_400
POOL_MULTIPLIER = 5
POOL_CAP = 500


class RankableMemory(Protocol):
    """支持 ACT-R 激活度排序的记忆项协议。"""

    @property
    def id(self) -> int: ...

    @property
    def created_at_epoch(self) -> int: ...

    @property
    def reinforcement_dates(self) -> tuple[str, ...]: ...


def iso_day_from_epoch(epoch_seconds: int) -> str:
    """将 Unix 秒级时间戳转为 UTC `YYYY-MM-DD`。"""
    return datetime.fromtimestamp(max(0, epoch_seconds), tz=UTC).strftime("%Y-%m-%d")


def _age_days_from_epoch(epoch_seconds: int, now_epoch: int) -> int:
    return max(1, (now_epoch - epoch_seconds) // SECONDS_PER_DAY)


def _age_days_from_iso(day_str: str, now_epoch: int) -> int:
    try:
        dt = datetime.strptime(day_str.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return 1
    return _age_days_from_epoch(int(dt.timestamp()), now_epoch)


def pool_size(count: int, alpha: float) -> int:
    """当启用 ACT-R 强化排序 (alpha > 0) 时放大候选池，让跨天强化的老核心知识浮出。"""
    if alpha <= 0.0 or count >= POOL_CAP:
        return count
    return min(POOL_CAP, max(count, count * POOL_MULTIPLIER))


def recency_head_size(count: int) -> int:
    """固定保护的最新头部窗口大小（ceil(count / 4)），确保刚发生的记忆绝不被老强化项挤占。"""
    return max(1, math.ceil(count / 4))


def blended_act_r_score(
    item: RankableMemory,
    *,
    now_epoch: int,
    alpha: float = 1.0,
) -> float:
    """计算 ACT-R 激活度得分: ln(1 + age_created^-d + alpha * sum(age_reinforcement^-d))。"""
    created_weight = math.pow(_age_days_from_epoch(item.created_at_epoch, now_epoch), -ACT_R_DECAY)
    creation_day = iso_day_from_epoch(item.created_at_epoch)
    reinforcement_sum = 0.0
    for day in set(item.reinforcement_dates):
        if day and day != creation_day:
            reinforcement_sum += math.pow(_age_days_from_iso(day, now_epoch), -ACT_R_DECAY)
    return math.log(1.0 + created_weight + alpha * reinforcement_sum)


def rank_by_strength[RankableT: RankableMemory](
    pool: Sequence[RankableT],
    count: int,
    *,
    alpha: float = 1.0,
    now_epoch: int | None = None,
) -> list[RankableT]:
    """从按时间降序排列的候选池中选出 Top-N 记忆，保护最新 1/4 头部并按时间降序输出。"""
    ordered_pool = sorted(pool, key=lambda x: (x.created_at_epoch, x.id), reverse=True)
    if len(ordered_pool) <= count or count <= 0:
        return ordered_pool[: max(0, count)]
    if alpha <= 0.0:
        return ordered_pool[:count]

    effective_now = (
        now_epoch if now_epoch is not None else int(datetime.now(tz=UTC).timestamp())
    )
    head_len = min(count, recency_head_size(count))
    head = ordered_pool[:head_len]
    tail_candidates = ordered_pool[head_len:]

    scored_tail = sorted(
        tail_candidates,
        key=lambda item: (
            blended_act_r_score(item, now_epoch=effective_now, alpha=alpha),
            item.created_at_epoch,
            item.id,
        ),
        reverse=True,
    )
    selected = [*head, *scored_tail[: count - head_len]]
    selected.sort(key=lambda x: (x.created_at_epoch, x.id), reverse=True)
    return selected
