"""Agent Loop 运行时安全守卫 Hook: 80% 预算软着陆预警、连续同参数 Doom-Loop 熔断与工具轨迹采集。"""

from __future__ import annotations

from typing import Any, override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from agent.core.budget import LoopBudgetPolicy, LoopSafetyAbortError
from agent.core.outcome_judge import (
    ToolCallRecord,
    ToolExecutionTrace,
    is_command_output_failed,
)
from agent.hooks.permission_hook import canonicalize_tool_args
from agent.hooks.priority import HookPriority

_MUTATING_TOOLS: frozenset[str] = frozenset(
    {"edit_file", "write_file", "create_directory", "save_memory"}
)
_COMMAND_TOOLS: frozenset[str] = frozenset({"shell", "run_command", "start_command"})

BUDGET_WRAPUP_WARNING = (
    "<budget_wrapup_warning>\n"
    "WARNING: You have reached >=80% of your execution budget (turns, tool calls, or tokens).\n"
    "Stop exploratory tool calls immediately. Finalize your current work and output a concise summary "
    "of what was completed and any remaining items, ending with `[TASK_STATUS: COMPLETED]` if the "
    "mandate is fulfilled or `[TASK_STATUS: INCOMPLETE]` if work remains.\n"
    "</budget_wrapup_warning>"
)


class LoopSafetyGuardHook(AbstractCapability[object]):
    """监控单次 Agent Loop 的预算软阈值、连续相同工具调用死循环与命令执行结果轨迹。"""

    hook_priority: int = int(HookPriority.LOOP_SAFETY)

    def __init__(
        self,
        *,
        policy: LoopBudgetPolicy,
        trace: ToolExecutionTrace | None = None,
    ) -> None:
        self._policy: LoopBudgetPolicy = policy
        self._trace: ToolExecutionTrace = trace if trace is not None else ToolExecutionTrace()
        self._soft_warned: bool = False
        self._last_tool_sig: tuple[str, str] | None = None
        self._identical_streak: int = 0

    @property
    def trace(self) -> ToolExecutionTrace:
        return self._trace

    @override
    async def before_model_request(
        self,
        ctx: RunContext[object],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        next_req_idx = ctx.usage.requests + 1
        if not self._soft_warned and self._policy.is_soft_limit_reached(
            ctx.usage, next_request_index=next_req_idx
        ):
            self._soft_warned = True
            self._trace.soft_limit_triggered = True
            _ = ctx.enqueue(BUDGET_WRAPUP_WARNING, priority="asap")
        return request_context

    @override
    async def before_tool_execute(
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        _ = (ctx, tool_def)
        canonical = canonicalize_tool_args(args)
        sig = (call.tool_name, canonical)

        if call.tool_name in _MUTATING_TOOLS:
            # 写操作会改变工作区状态，重置之前只读工具的重复调用计数
            self._last_tool_sig = sig
            self._identical_streak = 1
            return args

        if sig == self._last_tool_sig:
            self._identical_streak += 1
        else:
            self._last_tool_sig = sig
            self._identical_streak = 1

        if self._identical_streak >= self._policy.max_identical_tool_streak:
            self._trace.doom_loop_aborted = True
            raise LoopSafetyAbortError(
                f"Doom loop detected: tool `{call.tool_name}` called {self._identical_streak} "
                + f"consecutive times with identical arguments {canonical[:200]}",
                kind="doom_loop",
                tool_name=call.tool_name,
                streak=self._identical_streak,
            )
        return args

    @override
    async def after_tool_execute(  # pyright: ignore[reportAny]
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],  # pyright: ignore[reportExplicitAny]
        result: Any,  # pyright: ignore[reportExplicitAny, reportAny]
    ) -> Any:  # pyright: ignore[reportExplicitAny]
        _ = (ctx, tool_def)
        result_str = f"{result}" if result is not None else ""
        is_cmd = call.tool_name in _COMMAND_TOOLS
        cmd_failed = is_cmd and is_command_output_failed(result_str)
        self._trace.records.append(
            ToolCallRecord(
                tool_name=call.tool_name,
                canonical_args=canonicalize_tool_args(args),
                succeeded=True,
                is_command_tool=is_cmd,
                command_failed=cmd_failed,
                error_preview=result_str[:200] if cmd_failed else "",
            )
        )
        return result
