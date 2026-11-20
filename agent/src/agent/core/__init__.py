"""Agent 核心包:统一出口,调用方只从这里导入。"""

from agent.core.budget import (
    READONLY_ROLE_BUDGET_POLICY,
    READWRITE_ROLE_BUDGET_POLICY,
    ROUTER_BUDGET_POLICY,
    LoopAbortKind,
    LoopBudgetPolicy,
    LoopSafetyAbortError,
    default_role_budget_policy,
)
from agent.core.model import build_model
from agent.core.outcome_judge import (
    OutcomeReasonCode,
    TaskOutcomeVerdict,
    ToolCallRecord,
    ToolExecutionTrace,
    evaluate_task_outcome,
    is_command_output_failed,
)
from agent.core.roles import (
    RoleRegistry,
    RoleSessionManager,
    RoleSpec,
    RoleTicketIndex,
    build_default_role_registry,
)
from agent.core.router import DEFAULT_ROUTER_INSTRUCTIONS, RouterReply, SuperAgentRouter
from agent.core.runtime import (
    ContextSnapshot,
    SuperAgentRuntime,
    ask,
    configure_observability,
    open_runtime,
    run,
)
from agent.core.scheduler import TicketEventCallback, WorkspaceLockManager

__all__ = [
    "DEFAULT_ROUTER_INSTRUCTIONS",
    "READONLY_ROLE_BUDGET_POLICY",
    "READWRITE_ROLE_BUDGET_POLICY",
    "ROUTER_BUDGET_POLICY",
    "ContextSnapshot",
    "LoopAbortKind",
    "LoopBudgetPolicy",
    "LoopSafetyAbortError",
    "OutcomeReasonCode",
    "RoleRegistry",
    "RoleSessionManager",
    "RoleSpec",
    "RoleTicketIndex",
    "RouterReply",
    "SuperAgentRouter",
    "SuperAgentRuntime",
    "TaskOutcomeVerdict",
    "TicketEventCallback",
    "ToolCallRecord",
    "ToolExecutionTrace",
    "WorkspaceLockManager",
    "ask",
    "build_default_role_registry",
    "build_model",
    "configure_observability",
    "default_role_budget_policy",
    "evaluate_task_outcome",
    "is_command_output_failed",
    "open_runtime",
    "run",
]
