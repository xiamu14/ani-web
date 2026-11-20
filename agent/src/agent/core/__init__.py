"""Agent 核心包:统一出口,调用方只从这里导入。"""

from agent.core.model import build_model
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
    "ContextSnapshot",
    "RoleRegistry",
    "RoleSessionManager",
    "RoleSpec",
    "RoleTicketIndex",
    "RouterReply",
    "SuperAgentRouter",
    "SuperAgentRuntime",
    "TicketEventCallback",
    "WorkspaceLockManager",
    "ask",
    "build_default_role_registry",
    "build_model",
    "configure_observability",
    "open_runtime",
    "run",
]
