"""Agent 运行时: 装配双库、ContextTreeManager、SuperAgentRouter 与 Logfire 观测。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import logfire
from pydantic_ai._http import AsyncHTTPClient
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest
from pydantic_ai_harness.step_persistence import SqliteStepStore
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.attachments import AttachmentRegistry
from agent.config import LlmSettings, load_settings
from agent.context import ContextTreeManager, JevDecisionClient, SemanticBranchRouter
from agent.core.mcp_workspace import WorkspaceMcpManager
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
    CompressedFileMediaStore,
    SQLModelConversationStore,
    SQLModelMcpPolicyStore,
    SQLModelMemoryStore,
    SQLModelProtocolStore,
    SQLModelTicketStore,
    create_session_factory,
    create_sqlite_async_engine,
    default_db_path,
    init_async_db,
)
from agent.extensions import (
    FastMcpProbeAdapter,
    LocalOAuthReadinessChecker,
    McpElicitationHandler,
    McpServerSpec,
)
from agent.llm import LlmConcurrencyController
from agent.permissions import (
    JevMcpClassifier,
    McpClassifiedDecision,
    McpClassifierPort,
    McpCompileReport,
    McpPolicyCompiler,
    McpProbePort,
    McpToolPolicy,
    PermissionReviewer,
    ProbedMcpServer,
    ProbedMcpTool,
    UserApprovalPort,
)
from agent.protocol import (
    ContextAccountingHook,
    ContextWindowSnapshotView,
    InteractionBroker,
    RecoveredBranchView,
    RuntimeEventBus,
    RuntimeRecoveryPayload,
)
from agent.tools import RouterDeps, TaskDispatchPort, ToolLockCoordinator


class _FailSafeMcpClassifier:
    async def classify(
        self,
        server: ProbedMcpServer,
        tools: Sequence[ProbedMcpTool],
    ) -> Sequence[McpClassifiedDecision]:
        _ = (server, tools)
        raise RuntimeError(
            "Jev classifier is not configured (OPENROUTER_API_KEY missing)."
        )

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
    llm_controller: LlmConcurrencyController
    media_store: CompressedFileMediaStore
    attachment_registry: AttachmentRegistry
    scheduler: WorkspaceLockManager
    router: SuperAgentRouter
    logfire_enabled: bool
    event_bus: RuntimeEventBus | None = None
    interaction_broker: InteractionBroker | None = None
    protocol_store: SQLModelProtocolStore | None = None
    recovery_summary: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    mcp_compile_report: McpCompileReport | None = None
    mcp_specs: tuple[McpServerSpec, ...] = ()
    mcp_policies: Mapping[str, McpToolPolicy] = field(default_factory=lambda: cast(dict[str, McpToolPolicy], {}))
    mcp_blocked_servers: frozenset[str] = frozenset()
    mcp_manager: WorkspaceMcpManager | None = None

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
    elicitation_handler: McpElicitationHandler | None = None,
    mcp_http_client: AsyncHTTPClient | None = None,
    llm_controller: LlmConcurrencyController | None = None,
    media_dir: str | Path | None = None,
    attachment_registry: AttachmentRegistry | None = None,
    event_bus: RuntimeEventBus | None = None,
    interaction_broker: InteractionBroker | None = None,
    enable_protocol_services: bool = False,
) -> AsyncGenerator[SuperAgentRuntime]:
    """初始化双库、ContextTreeManager、Logfire 与 SuperAgentRouter。"""
    settings = load_settings()
    logfire_enabled = configure_observability(settings)

    step_target = Path(step_db_path).expanduser().resolve() if step_db_path is not None else default_step_db_path()
    step_target.parent.mkdir(parents=True, exist_ok=True)
    effective_media_dir = (
        Path(media_dir).expanduser().resolve()
        if media_dir is not None
        else step_target.parent / "media_blobs"
    )
    media_store = CompressedFileMediaStore(effective_media_dir)
    effective_registry = (
        attachment_registry
        if attachment_registry is not None
        else AttachmentRegistry(media_store=media_store)
    )

    engine = create_sqlite_async_engine(db_path)
    await init_async_db(engine)
    sessions = create_session_factory(engine)
    store = SQLModelConversationStore(engine, session_factory=sessions, media_store=media_store)
    ticket_store = SQLModelTicketStore(engine, session_factory=sessions)
    memory_store = SQLModelMemoryStore(engine, session_factory=sessions)

    step_store = SqliteStepStore(database=step_target)

    tree = ContextTreeManager(store, step_store=step_store, memory_store=memory_store)
    _ = await tree.bootstrap(global_agents_path=global_agents_path)
    use_protocol = enable_protocol_services or event_bus is not None or interaction_broker is not None
    owned_bus = event_bus is None and use_protocol
    eff_bus = event_bus if event_bus is not None else (RuntimeEventBus() if use_protocol else None)
    eff_broker = (
        interaction_broker
        if interaction_broker is not None
        else (InteractionBroker(event_bus=eff_bus, attachment_registry=effective_registry) if eff_bus is not None else None)
    )
    protocol_store = SQLModelProtocolStore(sessions) if use_protocol else None

    async def _on_jev_usage(u: Mapping[str, object]) -> None:
        if protocol_store is None:
            return
        cid = tree.active_conversation_id or "global"
        await protocol_store.record_aux_usage(
            conversation_id=cid, source="semantic_router",
            input_tokens=int(cast(int, u.get("prompt_tokens", 0)) or 0),
            output_tokens=int(cast(int, u.get("completion_tokens", 0)) or 0),
            total_tokens=int(cast(int, u.get("total_tokens", 0)) or 0),
        )

    effective_llm_ctrl = (
        llm_controller
        if llm_controller is not None
        else LlmConcurrencyController(
            role_max_concurrency=settings.llm_max_concurrency,
            router_max_concurrency=settings.router_max_concurrency,
            max_queued=settings.llm_max_queued,
            rate_limit_max_retries=settings.llm_rate_limit_max_retries,
            base_delay_sec=settings.llm_rate_limit_base_delay_sec,
        )
    )

    jev_client = (
        JevDecisionClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.decision_model_name,
            max_concurrency=settings.jev_max_concurrency,
            max_retries=settings.llm_rate_limit_max_retries,
            base_delay_sec=settings.llm_rate_limit_base_delay_sec,
            cooldown_gate=effective_llm_ctrl.cooldown_gate,
            on_usage=_on_jev_usage if protocol_store is not None else None,
        )
        if settings.openrouter_api_key
        else None
    )
    eff_approval = approval_port if approval_port is not None else eff_broker
    reviewer = (
        permission_reviewer
        if permission_reviewer is not None
        else PermissionReviewer(jev_client=jev_client, approval_port=eff_approval)
    )

    coordinator = tool_coordinator if tool_coordinator is not None else ToolLockCoordinator()
    raw_model = build_model(settings)
    role_model = effective_llm_ctrl.wrap_role_model(raw_model)
    router_model = effective_llm_ctrl.wrap_router_model(raw_model)
    roles = role_registry if role_registry is not None else build_default_role_registry(role_model)

    mcp_store = SQLModelMcpPolicyStore(engine, session_factory=sessions)
    effective_classifier: McpClassifierPort
    if mcp_classifier is not None:
        effective_classifier = mcp_classifier
    elif jev_client is not None:
        effective_classifier = JevMcpClassifier(jev_client)
    else:
        effective_classifier = _FailSafeMcpClassifier()

    mcp_compiler = McpPolicyCompiler(
        probe=mcp_probe if mcp_probe is not None else FastMcpProbeAdapter(token_root=mcp_token_root),
        classifier=effective_classifier,
        store=mcp_store,
        auth_readiness=LocalOAuthReadinessChecker(token_root=mcp_token_root),
    )
    mcp_manager = WorkspaceMcpManager(
        compiler=mcp_compiler, reviewer=reviewer,
        global_mcp_path=Path(global_mcp_path) if global_mcp_path is not None else None,
    )
    startup_mcp = await mcp_manager.ensure_compiled(mcp_workspace_root)
    mcp_compile_report, mcp_policy_index, mcp_blocked_servers, mcp_prefixes = (
        startup_mcp.report, startup_mcp.policies, startup_mcp.blocked_servers, startup_mcp.prefixes
    )

    eff_elicitation = (
        elicitation_handler
        if elicitation_handler is not None
        else (eff_broker.make_mcp_elicitation_handler(conversation_id=tree.active_conversation_id or "global") if eff_broker is not None else None)
    )

    async def _save_ctx_snap(snap: ContextWindowSnapshotView) -> None:
        if protocol_store is not None:
            await protocol_store.save_context_snapshot(
                conversation_id=snap.conversation_id, run_id=snap.run_id, model_name=snap.model_name,
                window_used_tokens=snap.window_used_tokens, window_limit_tokens=snap.window_limit_tokens,
                calibrated=snap.calibrated, snapshot_json=snap.model_dump_json(),
            )

    def _role_extra_caps(main_cid: str, task_id: str, role_name: str) -> Sequence[AbstractCapability[object]]:
        if eff_bus is None:
            return ()
        return (
            ContextAccountingHook(
                conversation_id=main_cid, task_id=task_id, role_name=role_name,
                model_name=settings.model_name, event_bus=eff_bus,
                known_mcp_prefixes=mcp_prefixes, on_snapshot=_save_ctx_snap,
            ),
        )

    role_sessions = RoleSessionManager(conversation_store=store, ticket_index=ticket_store)
    scheduler = WorkspaceLockManager(
        roles=roles, sessions=role_sessions, ticket_store=ticket_store, step_store=step_store,
        permission_reviewer=reviewer, tool_coordinator=coordinator, mcp_specs=startup_mcp.specs,
        mcp_policies=mcp_policy_index, mcp_blocked_servers=mcp_blocked_servers, mcp_prefixes=mcp_prefixes,
        mcp_token_root=mcp_token_root, elicitation_handler=eff_elicitation, mcp_http_client=mcp_http_client,
        mcp_manager=mcp_manager,
        memory_store=memory_store, llm_controller=effective_llm_ctrl, attachment_registry=effective_registry,
        event_bus=eff_bus, interaction_broker=eff_broker,
        extra_role_capabilities_factory=_role_extra_caps if eff_bus is not None else None,
    )

    semantic_router = SemanticBranchRouter(jev_client=jev_client) if jev_client is not None else None
    router_extra_caps: list[AbstractCapability[RouterDeps]] = []
    if eff_bus is not None:
        router_extra_caps.append(
            cast(
                AbstractCapability[RouterDeps],
                ContextAccountingHook(
                    conversation_id=tree.active_conversation_id or "global",
                    model_name=settings.model_name, event_bus=eff_bus,
                    known_mcp_prefixes=mcp_prefixes, on_snapshot=_save_ctx_snap,
                ),
            )
        )

    router = SuperAgentRouter(
        model=router_model, context_tree=tree, conversation_store=store,
        task_dispatcher=task_dispatcher if task_dispatcher is not None else scheduler,
        step_store=step_store, semantic_router=semantic_router, tool_coordinator=coordinator,
        event_bus=eff_bus, interaction_broker=eff_broker, attachment_registry=effective_registry,
        mcp_manager=mcp_manager,
        extra_capabilities=router_extra_caps,
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
    if eff_bus is not None and recovery_summary:
        _ = eff_bus.publish(
            "runtime.recovery",
            RuntimeRecoveryPayload(
                recovered_branches=[
                    RecoveredBranchView(
                        main_conversation_id=cid,
                        interrupted_task_ids=intr,
                        requeued_task_ids=req,
                    )
                    for cid, (intr, req) in recovery_summary.items()
                ]
            ),
        )
    try:
        yield SuperAgentRuntime(
            settings=settings, engine=engine, store=store, ticket_store=ticket_store,
            memory_store=memory_store, step_store=step_store, context_tree=tree,
            role_registry=roles, permission_reviewer=reviewer, tool_coordinator=coordinator,
            llm_controller=effective_llm_ctrl, media_store=media_store,
            attachment_registry=effective_registry, scheduler=scheduler, router=router,
            logfire_enabled=logfire_enabled, event_bus=eff_bus, interaction_broker=eff_broker,
            protocol_store=protocol_store, recovery_summary=recovery_summary,
            mcp_compile_report=mcp_compile_report, mcp_specs=startup_mcp.specs,
            mcp_policies=mcp_policy_index, mcp_blocked_servers=mcp_blocked_servers,
            mcp_manager=mcp_manager,
        )
    finally:
        if eff_broker is not None:
            _ = eff_broker.cancel_for_scope(reason="shutdown")
        await scheduler.wait_all()
        if owned_bus and eff_bus is not None:
            eff_bus.aclose()
        if jev_client is not None:
            await jev_client.aclose()
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
