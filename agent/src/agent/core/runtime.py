"""Agent 运行时: 装配双库、ContextTreeManager、SuperAgentRouter 与 Logfire 观测。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logfire
from fastmcp.client.elicitation import ElicitationHandler
from pydantic_ai._http import AsyncHTTPClient
from pydantic_ai.messages import ModelMessage, ModelRequest
from pydantic_ai_harness.step_persistence import SqliteStepStore
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.config import LlmSettings, load_settings
from agent.context import ContextTreeManager, JevDecisionClient, SemanticBranchRouter
from agent.core.model import build_model
from agent.core.roles import (
    RoleRegistry,
    RoleSessionManager,
    build_default_role_registry,
)
from agent.core.router import RouterReply, SuperAgentRouter
from agent.core.scheduler import WorkspaceLockManager
from agent.db import (
    ROOT_ENTRY_KEY,
    SQLModelConversationStore,
    SQLModelMcpPolicyStore,
    SQLModelMemoryStore,
    SQLModelTicketStore,
    create_session_factory,
    create_sqlite_async_engine,
    default_db_path,
    init_async_db,
)
from agent.extensions import (
    FastMcpProbeAdapter,
    LocalOAuthReadinessChecker,
    load_mcp_configs,
)
from agent.llm import JevHttpClient
from agent.permissions import (
    JevMcpClassifier,
    McpClassifiedDecision,
    McpClassifierPort,
    McpCompileReport,
    McpPolicyCompiler,
    McpProbePort,
    PermissionReviewer,
    ProbedMcpServer,
    ProbedMcpTool,
    UserApprovalPort,
)
from agent.tools import TaskDispatchPort, ToolLockCoordinator


class _FailSafeMcpClassifier:
    async def classify(
        self,
        server: ProbedMcpServer,
        tools: Sequence[ProbedMcpTool],
    ) -> Sequence[McpClassifiedDecision]:
        _ = server
        return [
            McpClassifiedDecision(
                tool_name=t.name,
                level="review",
                lock_mode="exclusive",
                recommended_roles=("coder",),
                reason="No Jev client configured; fail-safe review+exclusive",
            )
            for t in tools
        ]

_observability_state: set[str] = set()


def configure_observability(settings: LlmSettings) -> bool:
    """当配置了 LOGFIRE_TOKEN 时初始化 Logfire 与 Pydantic AI 链路追踪。"""
    if not settings.logfire_token:
        return False
    if "configured" not in _observability_state:
        _ = logfire.configure(
            token=settings.logfire_token,
            service_name="super-agent",
            console=False,
        )
        _ = logfire.instrument_pydantic_ai(include_content=True)
        _observability_state.add("configured")
    return True


def default_step_db_path() -> Path:
    """与 context_tree.db 物理隔离的 StepPersistence 数据库路径。"""
    return default_db_path().with_name("step_persistence.db")


@dataclass(frozen=True, kw_only=True)
class ContextSnapshot:
    """用于诊断与验收的数据库及 Context Window 快照。"""

    root_entry_id: str | None
    last_active_conversation_id: str | None
    conversation_id: str
    head_entry_id: str | None
    work_path: str | None
    revision: str
    requests: int
    input_tokens: int
    output_tokens: int
    tool_calls: int
    message_lines: tuple[str, ...]


def _summarize_message(index: int, msg: ModelMessage) -> str:
    role_label = "Request" if isinstance(msg, ModelRequest) else "Response"
    part_summaries: list[str] = []
    for part in msg.parts:
        raw_content = getattr(part, "content", None)
        if isinstance(raw_content, str):
            preview = raw_content.replace("\n", " ")[:40]
            part_summaries.append(f"{part.part_kind}({len(raw_content)}c: {preview!r})")
        else:
            part_summaries.append(part.part_kind)
    return f"  [{index}] {role_label}: {', '.join(part_summaries)}"


@dataclass(kw_only=True)
class SuperAgentRuntime:
    """装配完成的运行时容器。"""

    settings: LlmSettings
    engine: AsyncEngine
    store: SQLModelConversationStore
    ticket_store: SQLModelTicketStore
    memory_store: SQLModelMemoryStore
    step_store: SqliteStepStore
    context_tree: ContextTreeManager
    role_registry: RoleRegistry
    permission_reviewer: PermissionReviewer
    tool_coordinator: ToolLockCoordinator
    scheduler: WorkspaceLockManager
    router: SuperAgentRouter
    logfire_enabled: bool
    recovery_summary: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    mcp_compile_report: McpCompileReport | None = None

    async def inspect_context(self, conversation_id: str) -> ContextSnapshot:
        """读取真实持久化记录与当前 Context Window 内容。"""
        record = await self.store.read_record(conversation_id)
        if record is None:
            raise LookupError(f"Conversation {conversation_id!r} not found.")
        conv = await self.store.load(conversation_id, scope="main")
        if conv is None:
            raise LookupError(f"Conversation {conversation_id!r} failed to load.")

        work_path = await self.store.get_work_path(conversation_id)
        root_id = await self.store.get_app_state(ROOT_ENTRY_KEY)
        last_active = await self.store.get_last_active_conversation_id()
        lines = tuple(_summarize_message(idx, msg) for idx, msg in enumerate(conv.messages))
        usage = record.usage

        snap = ContextSnapshot(
            root_entry_id=root_id,
            last_active_conversation_id=last_active,
            conversation_id=conversation_id,
            head_entry_id=record.head_id,
            work_path=work_path,
            revision=record.revision,
            requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            tool_calls=usage.tool_calls,
            message_lines=lines,
        )
        if self.logfire_enabled:
            logfire.info(
                "context_window_snapshot",
                conversation_id=snap.conversation_id,
                head_entry_id=snap.head_entry_id,
                work_path=snap.work_path,
                message_count=len(snap.message_lines),
                input_tokens=snap.input_tokens,
                output_tokens=snap.output_tokens,
            )
        return snap


@asynccontextmanager
async def open_runtime(
    *,
    db_path: str | Path | None = None,
    step_db_path: str | Path | None = None,
    global_agents_path: str | Path | None = None,
    role_registry: RoleRegistry | None = None,
    task_dispatcher: TaskDispatchPort | None = None,
    approval_port: UserApprovalPort | None = None,
    permission_reviewer: PermissionReviewer | None = None,
    tool_coordinator: ToolLockCoordinator | None = None,
    global_mcp_path: str | Path | None = None,
    mcp_workspace_root: str | Path | None = None,
    mcp_token_root: Path | None = None,
    mcp_probe: McpProbePort | None = None,
    mcp_classifier: McpClassifierPort | None = None,
    elicitation_handler: ElicitationHandler[Any] | None = None,
    mcp_http_client: AsyncHTTPClient | None = None,
) -> AsyncGenerator[SuperAgentRuntime]:
    """初始化双库、ContextTreeManager、Logfire 与 SuperAgentRouter。"""
    settings = load_settings()
    logfire_enabled = configure_observability(settings)

    engine = create_sqlite_async_engine(db_path)
    await init_async_db(engine)
    sessions = create_session_factory(engine)
    store = SQLModelConversationStore(engine, session_factory=sessions)
    ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
    memory_store = SQLModelMemoryStore(engine, session_factory=sessions)

    step_target = Path(step_db_path).expanduser().resolve() if step_db_path is not None else default_step_db_path()
    step_target.parent.mkdir(parents=True, exist_ok=True)
    step_store = SqliteStepStore(database=step_target)

    tree = ContextTreeManager(store, step_store=step_store, memory_store=memory_store)
    _ = await tree.bootstrap(global_agents_path=global_agents_path)

    jev_client = (
        JevDecisionClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.decision_model_name,
        )
        if settings.openrouter_api_key
        else None
    )
    reviewer = (
        permission_reviewer
        if permission_reviewer is not None
        else PermissionReviewer(jev_client=jev_client, approval_port=approval_port)
    )

    coordinator = tool_coordinator if tool_coordinator is not None else ToolLockCoordinator()
    model = build_model(settings)
    roles = role_registry if role_registry is not None else build_default_role_registry(model)

    merged_mcp = load_mcp_configs(
        global_path=Path(global_mcp_path) if global_mcp_path is not None else None,
        workspace_root=Path(mcp_workspace_root) if mcp_workspace_root is not None else None,
    )
    mcp_store = SQLModelMcpPolicyStore(engine, session_factory=sessions)
    effective_classifier: McpClassifierPort
    if mcp_classifier is not None:
        effective_classifier = mcp_classifier
    elif settings.openrouter_api_key:
        effective_classifier = JevMcpClassifier(
            JevHttpClient(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
                model=settings.decision_model_name,
            )
        )
    else:
        effective_classifier = _FailSafeMcpClassifier()

    mcp_compiler = McpPolicyCompiler(
        probe=mcp_probe if mcp_probe is not None else FastMcpProbeAdapter(token_root=mcp_token_root),
        classifier=effective_classifier,
        store=mcp_store,
        auth_readiness=LocalOAuthReadinessChecker(token_root=mcp_token_root),
    )
    mcp_compile_report = await mcp_compiler.compile_all(merged_mcp.servers)
    mcp_policy_index = {p.prefixed_tool_name: p for p in mcp_compile_report.policies}
    reviewer.set_mcp_policies(mcp_policy_index)
    mcp_blocked_servers = frozenset(
        entry.server_name
        for entry in mcp_compile_report.entries
        if entry.status in ("auth_required", "error")
    )
    mcp_prefixes = tuple(spec.name for spec in merged_mcp.servers)

    role_sessions = RoleSessionManager(conversation_store=store, ticket_index=ticket_store)
    scheduler = WorkspaceLockManager(
        roles=roles,
        sessions=role_sessions,
        ticket_store=ticket_store,
        step_store=step_store,
        permission_reviewer=reviewer,
        tool_coordinator=coordinator,
        mcp_specs=merged_mcp.servers,
        mcp_policies=mcp_policy_index,
        mcp_blocked_servers=mcp_blocked_servers,
        mcp_prefixes=mcp_prefixes,
        mcp_token_root=mcp_token_root,
        elicitation_handler=elicitation_handler,
        mcp_http_client=mcp_http_client,
        memory_store=memory_store,
    )

    semantic_router = (
        SemanticBranchRouter(jev_client=jev_client)
        if jev_client is not None
        else None
    )

    router = SuperAgentRouter(
        model=model,
        context_tree=tree,
        conversation_store=store,
        task_dispatcher=task_dispatcher if task_dispatcher is not None else scheduler,
        step_store=step_store,
        semantic_router=semantic_router,
        tool_coordinator=coordinator,
    )
    recovery_summary: dict[str, tuple[list[str], list[str]]] = {}
    if task_dispatcher is None:
        scheduler.set_event_handler(
            lambda cid, summary, kind: router.on_ticket_event(cid, summary, kind=kind)
        )
        for branch in await tree.list_branches(limit=100):
            interrupted, requeued = await scheduler.recover_pending_tasks(
                branch.conversation_id,
                conversation_store=store,
            )
            if interrupted or requeued:
                recovery_summary[branch.conversation_id] = (interrupted, requeued)
            if interrupted:
                ids_text = ", ".join(f"@{tid}" for tid in interrupted)
                router.enqueue_system_notice(
                    branch.conversation_id,
                    (
                        f"进程重启恢复：{ids_text} 因上次退出中断已标记为 interrupted 并保留断点，"
                        + "请提示用户确认后对上游执行重试或取消。"
                    ),
                    kind="recovery",
                )
    try:
        yield SuperAgentRuntime(
            settings=settings,
            engine=engine,
            store=store,
            ticket_store=ticket_store,
            memory_store=memory_store,
            step_store=step_store,
            context_tree=tree,
            role_registry=roles,
            permission_reviewer=reviewer,
            tool_coordinator=coordinator,
            scheduler=scheduler,
            router=router,
            logfire_enabled=logfire_enabled,
            recovery_summary=recovery_summary,
            mcp_compile_report=mcp_compile_report,
        )
    finally:
        await scheduler.wait_all()
        await engine.dispose()
        if logfire_enabled:
            _ = logfire.force_flush()


async def ask(prompt: str, *, inspect: bool = False) -> RouterReply:
    """通过装配好的 SuperAgentRouter 执行一次对话请求，可选打印 Context Window 快照。"""
    async with open_runtime() as runtime:
        reply = await runtime.router.handle_user_message(prompt)
        if inspect:
            snap = await runtime.inspect_context(reply.conversation_id)
            print("\n=== Context Window & DB 状态 ===")
            print(f"root_entry_id: {snap.root_entry_id}")
            print(f"conversation_id: {snap.conversation_id} (last_active={snap.last_active_conversation_id})")
            print(f"head_entry_id: {snap.head_entry_id} | work_path: {snap.work_path}")
            print(
                f"usage: requests={snap.requests}, in={snap.input_tokens}, out={snap.output_tokens}, tools={snap.tool_calls}"
            )
            print("messages:")
            for line in snap.message_lines:
                print(line)
        return reply


def run(prompt: str, *, inspect: bool = True) -> None:
    """同步入口: 发送请求并打印模型回复与 Context Window 状态。"""
    reply = asyncio.run(ask(prompt, inspect=inspect))
    print(f"\n模型回复:\n{reply.output}")
