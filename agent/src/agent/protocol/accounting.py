"""Layer 1 Context Window 9 桶分桶计量 Hook、真实 Usage 校准与三层 Token 账单汇总。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext

from agent.core.budget import LoopBudgetPolicy
from agent.db.models import ConversationAuxUsageRow
from agent.hooks.priority import HookPriority
from agent.protocol.event_bus import RuntimeEventBus, utc_rfc3339
from agent.protocol.events_schema import (
    ContextSnapshotPayload,
    UsageBudgetWarningPayload,
    UsageStepRecordedPayload,
)
from agent.protocol.models import (
    AuxJevUsageView,
    ContextBucketCategory,
    ContextBucketView,
    ContextSubItemView,
    ContextWindowSnapshotView,
    RoleTaskUsageItemView,
    RunUsageView,
    TreeUsageSummaryView,
)
from agent.protocol.stream_bridge import to_run_usage_view
from agent.protocol.token_buckets import (
    BUCKET_ORDER,
    BucketAccumulator,
    count_tokens_text,
    estimate_context_buckets,
    estimate_model_context_limit,
)

__all__ = [
    "ContextAccountingHook",
    "build_tree_usage_summary",
    "count_tokens_text",
    "estimate_context_buckets",
    "estimate_model_context_limit",
]


class ContextAccountingHook(AbstractCapability[object]):
    """HookPriority.ACCOUNTING (85)：发送前 9 桶估算 + 响应后真实 Usage 校准。"""

    hook_priority: int = int(HookPriority.ACCOUNTING)

    def __init__(
        self,
        *,
        conversation_id: str,
        model_name: str,
        task_id: str | None = None,
        role_name: str | None = None,
        event_bus: RuntimeEventBus | None = None,
        budget_policy: LoopBudgetPolicy | None = None,
        known_mcp_prefixes: Collection[str] = (),
        on_snapshot: Callable[[ContextWindowSnapshotView], Awaitable[None]] | None = None,
    ) -> None:
        self._conversation_id: str = conversation_id
        self._model_name: str = model_name
        self._task_id: str | None = task_id
        self._role_name: str | None = role_name
        self._bus: RuntimeEventBus | None = event_bus
        self._budget_policy: LoopBudgetPolicy | None = budget_policy
        self._mcp_prefixes: tuple[str, ...] = tuple(
            p if p.endswith("_") else f"{p}_" for p in known_mcp_prefixes if p
        )
        self._on_snapshot: Callable[[ContextWindowSnapshotView], Awaitable[None]] | None = on_snapshot
        self._last_acc: BucketAccumulator | None = None
        self._last_snapshot: ContextWindowSnapshotView | None = None
        self._soft_warned_bus: bool = False

    @property
    def last_snapshot(self) -> ContextWindowSnapshotView | None:
        return self._last_snapshot

    @override
    async def before_model_request(
        self, ctx: RunContext[object], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        acc, enc_name = estimate_context_buckets(
            request_context, model_name=self._model_name, mcp_prefixes=self._mcp_prefixes
        )
        self._last_acc = acc
        est_total = sum(acc.totals.values())
        snap = self._build_snapshot(
            acc=acc,
            enc_name=enc_name,
            run_id=ctx.run_id or "run",
            window_used_tokens=est_total,
            cache_read_tokens=0,
            cache_write_tokens=0,
            calibrated=False,
            estimation_delta=0,
            scaled=False,
        )
        self._last_snapshot = snap
        if self._bus is not None:
            _ = self._bus.publish(
                "context.snapshot",
                ContextSnapshotPayload(snapshot=snap),
                conversation_id=self._conversation_id,
                task_id=self._task_id,
                role_name=self._role_name,
                run_id=ctx.run_id or None,
            )
        return request_context

    @override
    async def after_model_request(
        self,
        ctx: RunContext[object],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        _ = request_context
        acc = self._last_acc
        if acc is None:
            return response
        usage = response.usage
        # 严格遵循 pydantic_ai UsageBase.input_tokens 包含式口径（已含 cache_read & cache_write）
        actual_input = usage.input_tokens or 0
        est_non_overhead = sum(v for k, v in acc.totals.items() if k != "overhead")
        delta = actual_input - est_non_overhead
        scaled = False
        if actual_input > 0 and delta >= 0:
            acc.totals["overhead"] = delta
            acc.sub_items["overhead"]["framing_and_tokenizer_delta"] = delta
        elif actual_input > 0 and est_non_overhead > 0:
            scaled = True
            ratio = actual_input / est_non_overhead
            running_sum = 0
            non_zero_keys: list[ContextBucketCategory] = [
                k for k in BUCKET_ORDER if k != "overhead" and acc.totals[k] > 0
            ]
            for idx, k in enumerate(non_zero_keys):
                if idx == len(non_zero_keys) - 1:
                    acc.totals[k] = max(0, actual_input - running_sum)
                else:
                    scaled_val = round(acc.totals[k] * ratio)
                    acc.totals[k] = scaled_val
                    running_sum += scaled_val
            acc.totals["overhead"] = 0

        snap = self._build_snapshot(
            acc=acc,
            enc_name=acc.enc_name,
            run_id=ctx.run_id or "run",
            window_used_tokens=actual_input if actual_input > 0 else est_non_overhead,
            cache_read_tokens=usage.cache_read_tokens or 0,
            cache_write_tokens=usage.cache_write_tokens or 0,
            calibrated=actual_input > 0,
            estimation_delta=delta,
            scaled=scaled,
        )
        self._last_snapshot = snap
        if self._on_snapshot is not None:
            await self._on_snapshot(snap)
        if self._bus is not None:
            _ = self._bus.publish(
                "context.snapshot",
                ContextSnapshotPayload(snapshot=snap),
                conversation_id=self._conversation_id,
                task_id=self._task_id,
                role_name=self._role_name,
                run_id=ctx.run_id or None,
            )
            run_view = to_run_usage_view(ctx.usage, budget_policy=self._budget_policy)
            step_view = RunUsageView(
                input_tokens=actual_input,
                cache_read_tokens=usage.cache_read_tokens or 0,
                cache_write_tokens=usage.cache_write_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                output_reasoning_tokens=usage.output_reasoning_tokens or 0,
                total_tokens=actual_input + (usage.output_tokens or 0),
                requests=1,
            )
            _ = self._bus.publish(
                "usage.step_recorded",
                UsageStepRecordedPayload(
                    conversation_id=self._conversation_id,
                    task_id=self._task_id,
                    run_id=ctx.run_id or "run",
                    step_usage=step_view,
                    run_usage=run_view,
                ),
                conversation_id=self._conversation_id,
                task_id=self._task_id,
                role_name=self._role_name,
                run_id=ctx.run_id or None,
            )
            if (
                not self._soft_warned_bus
                and self._budget_policy is not None
                and self._budget_policy.is_soft_limit_reached(ctx.usage)
            ):
                self._soft_warned_bus = True
                _ = self._bus.publish(
                    "usage.budget_warning",
                    UsageBudgetWarningPayload(
                        conversation_id=self._conversation_id,
                        task_id=self._task_id,
                        run_id=ctx.run_id or "run",
                        dimension="tokens",
                        ratio=self._budget_policy.soft_limit_ratio,
                    ),
                    conversation_id=self._conversation_id,
                    task_id=self._task_id,
                    role_name=self._role_name,
                    run_id=ctx.run_id or None,
                )
        return response

    def _build_snapshot(
        self,
        *,
        acc: BucketAccumulator,
        enc_name: str,
        run_id: str,
        window_used_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        calibrated: bool,
        estimation_delta: int,
        scaled: bool,
    ) -> ContextWindowSnapshotView:
        limit_val, limit_known = estimate_model_context_limit(self._model_name)
        denom = max(1, window_used_tokens)
        buckets = [
            ContextBucketView(
                category=cat,
                tokens=acc.totals[cat],
                ratio=round(acc.totals[cat] / denom, 4) if window_used_tokens > 0 else 0.0,
                sub_items=[
                    ContextSubItemView(name=sn, tokens=st, scaled=scaled and cat != "overhead")
                    for sn, st in acc.sub_items[cat].items()
                ],
            )
            for cat in BUCKET_ORDER
        ]
        uncached = max(0, window_used_tokens - cache_read_tokens - cache_write_tokens)
        return ContextWindowSnapshotView(
            conversation_id=self._conversation_id,
            task_id=self._task_id,
            run_id=run_id,
            model_name=self._model_name,
            window_used_tokens=window_used_tokens,
            window_limit_tokens=limit_val,
            limit_known=limit_known,
            utilization_ratio=(
                round(window_used_tokens / limit_val, 4)
                if limit_val is not None and limit_val > 0
                else 0.0
            ),
            cache_hit_ratio=round(cache_read_tokens / denom, 4) if window_used_tokens > 0 else 0.0,
            uncached_input_tokens=uncached,
            calibrated=calibrated,
            tokenizer_name=enc_name,
            estimation_delta=estimation_delta,
            categories=buckets,
            savings=acc.savings,
            ts=utc_rfc3339(),
        )


def build_tree_usage_summary(
    *,
    conversation_id: str,
    main_usage: RunUsageView,
    role_items: Sequence[RoleTaskUsageItemView],
    aux_rows: Sequence[ConversationAuxUsageRow],
) -> TreeUsageSummaryView:
    """汇总 Layer C Conversation Tree 全局用量（主会话 + Role 子任务 + Jev 辅助用量）。"""
    role_total = RunUsageView(
        input_tokens=sum(r.usage.input_tokens for r in role_items),
        cache_read_tokens=sum(r.usage.cache_read_tokens for r in role_items),
        cache_write_tokens=sum(r.usage.cache_write_tokens for r in role_items),
        output_tokens=sum(r.usage.output_tokens for r in role_items),
        output_reasoning_tokens=sum(r.usage.output_reasoning_tokens for r in role_items),
        total_tokens=sum(r.usage.total_tokens for r in role_items),
        requests=sum(r.usage.requests for r in role_items),
        tool_calls=sum(r.usage.tool_calls for r in role_items),
    )
    aux_views = [
        AuxJevUsageView(
            source=(
                row.source
                if row.source in ("semantic_router", "permission_review", "mcp_compile", "other")
                else "other"
            ),
            requests=row.requests,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            cost_usd=row.cost_usd,
        )
        for row in aux_rows
    ]
    return TreeUsageSummaryView(
        conversation_id=conversation_id,
        main_router_usage=main_usage,
        role_tasks_usage=list(role_items),
        role_tasks_total=role_total,
        auxiliary_jev_usage=aux_views,
    )
