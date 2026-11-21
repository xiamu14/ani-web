"""统一运行期错误捕获与 Logfire 上报 Hook（基于 Pydantic AI 原生 AbstractCapability）。"""

from __future__ import annotations

import asyncio
from typing import override

import logfire
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import RunCancelled
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import ToolDefinition

from agent.hooks.priority import HookPriority


def _format_single_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def format_exception_detail(exc: BaseException) -> str:
    """将任意异常（含空消息异常及底层 cause/context）格式化为非空可读摘要。"""
    head = _format_single_exception(exc)
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        cause_str = _format_single_exception(cause)
        if cause_str != head:
            return f"{head} (cause: {cause_str})"
    return head


class RunErrorReportingHook(AbstractCapability[object]):
    """利用 Pydantic AI 原生错误 Hook 统一记录工具、模型请求与 Run 级异常并原样传播。"""

    hook_priority: int = int(HookPriority.OBSERVABILITY)

    def __init__(
        self,
        *,
        agent_name: str,
        conversation_id: str = "",
        task_id: str | None = None,
        role_name: str | None = None,
    ) -> None:
        self._agent_name: str = agent_name
        self._conversation_id: str = conversation_id
        self._task_id: str | None = task_id
        self._role_name: str | None = role_name

    def _resolve_conversation_id(self, ctx: RunContext[object]) -> str:
        if self._conversation_id:
            return self._conversation_id
        return str(getattr(ctx.deps, "main_conversation_id", "") or "")

    @override
    async def on_tool_execute_error(
        self,
        ctx: RunContext[object],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, object],
        error: Exception,
    ) -> object:
        _ = (tool_def, args)
        logfire.error(
            "Tool execution failed",
            agent_name=self._agent_name,
            conversation_id=self._resolve_conversation_id(ctx),
            task_id=self._task_id,
            role_name=self._role_name,
            run_id=ctx.run_id,
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            error_type=type(error).__name__,
            error=format_exception_detail(error),
        )
        raise error

    @override
    async def on_model_request_error(
        self,
        ctx: RunContext[object],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        logfire.error(
            "Model request failed",
            agent_name=self._agent_name,
            conversation_id=self._resolve_conversation_id(ctx),
            task_id=self._task_id,
            role_name=self._role_name,
            run_id=ctx.run_id,
            model_name=request_context.model.model_name,
            error_type=type(error).__name__,
            error=format_exception_detail(error),
        )
        raise error

    @override
    async def on_run_error(
        self,
        ctx: RunContext[object],
        *,
        error: BaseException,
    ) -> AgentRunResult[object]:
        if isinstance(error, (asyncio.CancelledError, RunCancelled)):
            raise error
        logfire.error(
            "Agent run failed",
            agent_name=self._agent_name,
            conversation_id=self._resolve_conversation_id(ctx),
            task_id=self._task_id,
            role_name=self._role_name,
            run_id=ctx.run_id,
            error_type=type(error).__name__,
            error=format_exception_detail(error),
        )
        error.add_note("_logged_by_error_hook")
        raise error
