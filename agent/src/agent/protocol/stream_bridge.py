"""Pydantic AI AgentStreamEvent -> RuntimeEventBus 流式桥接与用量/溯源视图转换器。"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai.usage import RunUsage

from agent.attachments.citations import extract_and_verify_citations
from agent.attachments.ports import AttachmentRegistryPort
from agent.core.budget import LoopBudgetPolicy
from agent.protocol.event_bus import RuntimeEventBus
from agent.protocol.events_schema import (
    EventChannelLiteral,
    MessagePartEndPayload,
    MessagePartStartPayload,
    PartKindLiteral,
)
from agent.protocol.models import (
    BudgetProgressView,
    CitationLocatorView,
    RunUsageView,
    VerifiedCitationView,
)


def _part_kind_for(part: object) -> PartKindLiteral:
    if isinstance(part, ThinkingPart):
        return "thinking"
    if isinstance(part, ToolCallPart):
        return "tool_call"
    return "text"


def _initial_text_for(part: object) -> str:
    if isinstance(part, (TextPart, ThinkingPart)):
        return part.content
    return ""


def make_event_stream_handler(
    bus: RuntimeEventBus,
    *,
    conversation_id: str,
    task_id: str | None = None,
    role_name: str | None = None,
    proactive: bool = False,
    channel: EventChannelLiteral = "main",
    on_tool_call: Callable[[str, str, str], None] | None = None,
    on_tool_result: Callable[[str, bool, str], None] | None = None,
) -> Callable[[RunContext[object], AsyncIterable[AgentStreamEvent]], Awaitable[None]]:
    """构建可直接传入 Agent.run(..., event_stream_handler=...) 的流式事件转发器。"""

    async def _handler(
        ctx: RunContext[object], stream: AsyncIterable[AgentStreamEvent]
    ) -> None:
        run_id = ctx.run_id or None
        active_kinds: dict[int, PartKindLiteral] = {}

        async for event in stream:
            if isinstance(event, PartStartEvent):
                pkind = _part_kind_for(event.part)
                active_kinds[event.index] = pkind
                tool_name = (
                    event.part.tool_name
                    if isinstance(event.part, ToolCallPart)
                    else None
                )
                tool_call_id = (
                    event.part.tool_call_id
                    if isinstance(event.part, ToolCallPart)
                    else None
                )
                _ = bus.publish(
                    "message.part.start",
                    MessagePartStartPayload(
                        part_index=event.index,
                        part_kind=pkind,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        proactive=proactive,
                    ),
                    conversation_id=conversation_id,
                    task_id=task_id,
                    role_name=role_name,
                    run_id=run_id,
                    channel=channel,
                )
                initial = _initial_text_for(event.part)
                if initial:
                    bus.emit_delta(
                        initial,
                        part_index=event.index,
                        part_kind=pkind,
                        tool_call_id=tool_call_id,
                        proactive=proactive,
                        conversation_id=conversation_id,
                        task_id=task_id,
                        role_name=role_name,
                        run_id=run_id,
                        channel=channel,
                    )
            elif isinstance(event, PartDeltaEvent):
                delta_obj = event.delta
                pkind = active_kinds.get(event.index, "text")
                delta_text = ""
                tool_call_id = None
                if isinstance(delta_obj, TextPartDelta):
                    pkind = "text"
                    delta_text = delta_obj.content_delta
                elif isinstance(delta_obj, ThinkingPartDelta):
                    pkind = "thinking"
                    delta_text = delta_obj.content_delta or ""
                elif isinstance(delta_obj, ToolCallPartDelta):
                    pkind = "tool_call"
                    tool_call_id = delta_obj.tool_call_id
                    args_delta = delta_obj.args_delta
                    delta_text = (
                        args_delta
                        if isinstance(args_delta, str)
                        else str(args_delta or "")
                    )
                if delta_text:
                    bus.emit_delta(
                        delta_text,
                        part_index=event.index,
                        part_kind=pkind,
                        tool_call_id=tool_call_id,
                        proactive=proactive,
                        conversation_id=conversation_id,
                        task_id=task_id,
                        role_name=role_name,
                        run_id=run_id,
                        channel=channel,
                    )
            elif isinstance(event, PartEndEvent):
                pkind = active_kinds.get(event.index, _part_kind_for(event.part))
                tool_name = (
                    event.part.tool_name
                    if isinstance(event.part, ToolCallPart)
                    else None
                )
                tool_call_id = (
                    event.part.tool_call_id
                    if isinstance(event.part, ToolCallPart)
                    else None
                )
                _ = bus.publish(
                    "message.part.end",
                    MessagePartEndPayload(
                        part_index=event.index,
                        part_kind=pkind,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        proactive=proactive,
                    ),
                    conversation_id=conversation_id,
                    task_id=task_id,
                    role_name=role_name,
                    run_id=run_id,
                    channel=channel,
                )
            elif isinstance(event, FunctionToolCallEvent):
                args_str = event.part.args_as_json_str()
                if on_tool_call is not None:
                    on_tool_call(
                        event.part.tool_call_id,
                        event.part.tool_name,
                        args_str[:160],
                    )
            elif isinstance(event, FunctionToolResultEvent):
                res_preview = str(event.part.content)[:200]
                is_err = str(event.part.part_kind) == "retry-prompt"
                if on_tool_result is not None:
                    on_tool_result(
                        event.part.tool_name or "",
                        not is_err,
                        res_preview if is_err else "",
                    )
                _ = bus.publish(
                    "message.part.end",
                    MessagePartEndPayload(
                        part_index=9999,
                        part_kind="tool_result",
                        tool_name=event.part.tool_name,
                        tool_call_id=event.part.tool_call_id,
                        result_preview=res_preview,
                        is_error=is_err,
                        proactive=proactive,
                    ),
                    conversation_id=conversation_id,
                    task_id=task_id,
                    role_name=role_name,
                    run_id=run_id,
                    channel=channel,
                )

    return _handler


def verify_citations_for_protocol(
    output_text: str, registry: AttachmentRegistryPort | None
) -> list[VerifiedCitationView]:
    """将输出文本中的 [[cite:...]] 校验结果转换为协议层强类型视图列表。"""
    if registry is None or "[[cite:" not in output_text:
        return []
    verdicts = extract_and_verify_citations(output_text, registry)
    return [
        VerifiedCitationView(
            raw_tag=v.raw_tag,
            target_kind=v.target_kind,
            target_id=v.target_id,
            locator=CitationLocatorView(
                page=v.locator.page,
                line_start=v.locator.line_start,
                line_end=v.locator.line_end,
                time_start_sec=v.locator.time_start_sec,
                time_end_sec=v.locator.time_end_sec,
                bbox=list(v.locator.bbox) if v.locator.bbox is not None else None,
                artifact_key_path=v.locator.artifact_key_path,
            ),
            excerpt=v.excerpt,
            verified=v.verified,
            verification_reason=v.verification_reason,
        )
        for v in verdicts
    ]


def to_run_usage_view(
    usage: RunUsage, *, budget_policy: LoopBudgetPolicy | None = None
) -> RunUsageView:
    """将 Pydantic AI RunUsage 转换为协议层 RunUsageView（严格保持 input_tokens 包含式口径）。"""
    total_tok = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    raw_cost = getattr(usage, "cost", None)
    cost_val = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    progress: BudgetProgressView | None = None
    if budget_policy is not None:
        progress = BudgetProgressView(
            requests_ratio=min(1.0, (usage.requests or 0) / max(1, budget_policy.max_requests)),
            tool_calls_ratio=min(1.0, (usage.tool_calls or 0) / max(1, budget_policy.max_tool_calls)),
            tokens_ratio=min(1.0, total_tok / max(1, budget_policy.max_total_tokens)),
            soft_limit_reached=budget_policy.is_soft_limit_reached(usage),
        )
    return RunUsageView(
        input_tokens=usage.input_tokens or 0,
        cache_read_tokens=usage.cache_read_tokens or 0,
        cache_write_tokens=usage.cache_write_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        output_reasoning_tokens=usage.output_reasoning_tokens or 0,
        total_tokens=total_tok,
        cost_usd=cost_val,
        requests=usage.requests or 0,
        tool_calls=usage.tool_calls or 0,
        budget_progress=progress,
    )
