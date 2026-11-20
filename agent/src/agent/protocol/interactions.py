"""Layer 1 统一交互中心（InteractionBroker）：逐条权限审核、AskUser 与 MCP Elicitation 挂起桥接。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from fastmcp.client._sdk_context_shim import LifespanContextT, RequestContext
from fastmcp.client.elicitation import ElicitRequestParams, ElicitResult
from mcp import ClientSession
from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.messages import TextContent
from pydantic_ai_harness.ask_user import (
    AskUserAnswer,
    AskUserRequest,
    AskUserResponse,
    check_response,
)

from agent.attachments.ports import AttachmentRegistryPort
from agent.extensions.mcp_loader import McpElicitationHandler
from agent.permissions.reviewer import (
    ApprovalContext,
    ApprovalDecision,
    ApprovalItem,
    ItemApprovalDecision,
)
from agent.protocol.event_bus import RuntimeEventBus, utc_rfc3339
from agent.protocol.events_schema import (
    InteractionCancelledPayload,
    InteractionRequestedPayload,
    InteractionResolvedPayload,
)
from agent.protocol.models import (
    ApprovalContextView,
    ApprovalItemView,
    ApprovalPayloadView,
    ApprovalRespondPayload,
    AskUserOptionView,
    AskUserPayloadView,
    AskUserQuestionView,
    AskUserRespondPayload,
    InteractionRespondRequest,
    InteractionView,
    McpElicitationPayloadView,
    McpElicitationRespondPayload,
)


class InteractionNotFoundError(LookupError):
    """指定 interaction_id 不存在。"""


class InteractionAlreadyResolvedError(RuntimeError):
    """指定 interaction_id 已被响应或取消，不可重复提交。"""


class InteractionPayloadError(ValueError):
    """提交的交互响应类型或结构不合法。"""


_HIGH_RISK_CATEGORIES: frozenset[str] = frozenset(
    {
        "critical_config_write",
        "shell_mutation",
        "external_network_or_pkg",
        "mcp_schema_drift",
        "out_of_workspace_shell",
    }
)


def _infer_risk_level(item: ApprovalItem) -> Literal["low", "medium", "high"]:
    if item.category in _HIGH_RISK_CATEGORIES:
        return "high"
    if item.category in ("out_of_workspace_read", "mcp_unregistered"):
        return "medium"
    return "medium"


def _to_json_value(val: object) -> JsonValue:
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    if isinstance(val, (list, tuple)):
        return [_to_json_value(item) for item in cast(Sequence[object], val)]
    if isinstance(val, Mapping):
        return _to_json_dict(cast(Mapping[str, object], val))
    return str(val)


def _to_json_dict(raw: Mapping[str, object]) -> dict[str, JsonValue]:
    return {str(k): _to_json_value(v) for k, v in raw.items()}


@dataclass
class _PendingInteraction:
    view: InteractionView
    future: asyncio.Future[object]
    ask_user_request: AskUserRequest | None = None
    approval_items: tuple[ApprovalItem, ...] = ()


class InteractionBroker:
    """统一管理阻塞式人机交互（Approval 逐条勾选 / AskUser 选择建议 / MCP Elicitation）。"""

    def __init__(
        self,
        *,
        event_bus: RuntimeEventBus,
        attachment_registry: AttachmentRegistryPort | None = None,
    ) -> None:
        self._bus: RuntimeEventBus = event_bus
        self._attachments: AttachmentRegistryPort | None = attachment_registry
        self._pending: dict[str, _PendingInteraction] = {}
        self._resolved_ids: set[str] = set()

    def list_pending(
        self,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
    ) -> list[InteractionView]:
        """列出当前所有未决交互（支持按会话或任务过滤）。"""
        result: list[InteractionView] = []
        for entry in self._pending.values():
            if conversation_id is not None and entry.view.conversation_id != conversation_id:
                continue
            if task_id is not None and entry.view.task_id != task_id:
                continue
            result.append(entry.view)
        return result

    def pending_ids_for_task(self, main_conversation_id: str, task_id: str) -> list[str]:
        return [
            entry.view.interaction_id
            for entry in self._pending.values()
            if entry.view.conversation_id == main_conversation_id
            and entry.view.task_id == task_id
        ]

    def get_interaction(self, interaction_id: str) -> InteractionView | None:
        entry = self._pending.get(interaction_id)
        return entry.view if entry is not None else None

    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision:
        """实现 UserApprovalPort（无上下文时回退）。"""
        return await self.request_batch_approval_with_context(
            items, user_intent=user_intent, context=None
        )

    def _publish_interaction(
        self, event_type: str, payload: InteractionRequestedPayload | InteractionResolvedPayload | InteractionCancelledPayload, view: InteractionView
    ) -> None:
        _ = self._bus.publish(
            event_type,
            payload,
            conversation_id=view.conversation_id,
            task_id=view.task_id,
            role_name=view.role_name,
            run_id=view.run_id,
        )

    async def request_batch_approval_with_context(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
        context: ApprovalContext | None = None,
    ) -> ApprovalDecision:
        """实现 ContextualUserApprovalPort：将同轮批审批推送至 GUI 并等待逐条裁决。"""
        ctx = context or ApprovalContext(user_intent=user_intent)
        interaction_id = f"int_{uuid4().hex[:12]}"
        view = InteractionView(
            interaction_id=interaction_id,
            kind="approval",
            status="pending",
            conversation_id=ctx.main_conversation_id or "default",
            task_id=ctx.task_id,
            role_name=ctx.role_name,
            run_id=ctx.run_id or None,
            created_at=utc_rfc3339(),
            approval=ApprovalPayloadView(
                items=[
                    ApprovalItemView(
                        item_id=item.item_id,
                        tool_name=item.tool_name,
                        args=_to_json_dict(item.args),
                        category=item.category,
                        rule_id=item.rule_id,
                        reason=item.reason,
                        risk_level=_infer_risk_level(item),
                    )
                    for item in items
                ],
                context=ApprovalContextView(
                    main_conversation_id=ctx.main_conversation_id or "default",
                    task_id=ctx.task_id,
                    role_name=ctx.role_name,
                    run_id=ctx.run_id,
                    user_intent=ctx.user_intent or user_intent,
                ),
            ),
        )
        fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[interaction_id] = _PendingInteraction(
            view=view, future=fut, approval_items=tuple(items)
        )
        self._publish_interaction(
            "interaction.requested", InteractionRequestedPayload(interaction=view), view
        )
        return cast(ApprovalDecision, await fut)

    async def answer_ask_user(
        self,
        request: AskUserRequest,
        *,
        conversation_id: str,
        task_id: str | None = None,
        role_name: str | None = None,
        run_id: str | None = None,
    ) -> AskUserResponse:
        """将 AskUserRequest 推送至 GUI 并挂起等待用户选择或自定义回答。"""
        interaction_id = f"int_{request.id[:16]}"
        view = InteractionView(
            interaction_id=interaction_id,
            kind="ask_user",
            status="pending",
            conversation_id=conversation_id,
            task_id=task_id,
            role_name=role_name,
            run_id=run_id,
            created_at=utc_rfc3339(),
            ask_user=AskUserPayloadView(
                request_id=request.id,
                questions=[
                    AskUserQuestionView(
                        header=q.header,
                        question=q.question,
                        options=[
                            AskUserOptionView(label=opt.label, description=opt.description)
                            for opt in q.options
                        ],
                        multi_select=q.multi_select,
                    )
                    for q in request.questions
                ],
            ),
        )
        fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[interaction_id] = _PendingInteraction(
            view=view, future=fut, ask_user_request=request
        )
        self._publish_interaction(
            "interaction.requested", InteractionRequestedPayload(interaction=view), view
        )
        return cast(AskUserResponse, await fut)

    def make_ask_user_answerer(
        self,
        *,
        conversation_id: str,
        task_id: str | None = None,
        role_name: str | None = None,
    ) -> Callable[[RunContext[object], AskUserRequest], Awaitable[AskUserResponse]]:
        """生成可直接注入 pydantic_ai_harness.ask_user.AskUser 的 ContextAnswerer。"""

        async def _answerer(
            ctx: RunContext[object], request: AskUserRequest
        ) -> AskUserResponse:
            return await self.answer_ask_user(
                request,
                conversation_id=conversation_id,
                task_id=task_id,
                role_name=role_name,
                run_id=ctx.run_id or None,
            )

        return _answerer

    def make_mcp_elicitation_handler(
        self,
        *,
        conversation_id: str,
        task_id: str | None = None,
        role_name: str | None = None,
        server_name: str = "mcp",
    ) -> McpElicitationHandler:
        """生成可注入 MCPToolset 的 McpElicitationHandler。"""

        async def _handler(
            message: str,
            _response_type: type[object] | None,
            params: ElicitRequestParams,
            _context: RequestContext[ClientSession, LifespanContextT],
        ) -> ElicitResult[dict[str, object]]:
            raw_schema = getattr(params, "requestedSchema", None)
            schema_dict = (
                _to_json_dict(cast(Mapping[str, object], raw_schema))
                if isinstance(raw_schema, Mapping)
                else {}
            )
            interaction_id = f"int_{uuid4().hex[:12]}"
            view = InteractionView(
                interaction_id=interaction_id,
                kind="mcp_elicitation",
                status="pending",
                conversation_id=conversation_id,
                task_id=task_id,
                role_name=role_name,
                created_at=utc_rfc3339(),
                mcp_elicitation=McpElicitationPayloadView(
                    server_name=server_name,
                    message=message,
                    requested_schema=schema_dict,
                ),
            )
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[object] = loop.create_future()
            self._pending[interaction_id] = _PendingInteraction(view=view, future=fut)
            self._publish_interaction(
                "interaction.requested",
                InteractionRequestedPayload(interaction=view),
                view,
            )
            result = await fut
            return cast(ElicitResult[dict[str, object]], result)

        return cast(McpElicitationHandler, _handler)

    async def respond(
        self,
        interaction_id: str,
        payload: InteractionRespondRequest,
        *,
        resolved_by: str = "user",
    ) -> InteractionView:
        """校验并提交人工交互结果，唤醒挂起协程并广播 interaction.resolved。"""
        entry = self._pending.get(interaction_id)
        if entry is None:
            if interaction_id in self._resolved_ids:
                raise InteractionAlreadyResolvedError(
                    f"Interaction {interaction_id!r} already resolved or cancelled."
                )
            raise InteractionNotFoundError(
                f"Interaction {interaction_id!r} not found."
            )

        if payload.kind != entry.view.kind:
            raise InteractionPayloadError(
                f"Interaction {interaction_id!r} expects kind={entry.view.kind!r}, got {payload.kind!r}."
            )

        native_result, summary = self._convert_response(entry, payload)
        _ = self._pending.pop(interaction_id, None)
        self._resolved_ids.add(interaction_id)
        resolved_view = entry.view.model_copy(update={"status": "resolved"})

        if not entry.future.done():
            entry.future.set_result(native_result)

        self._publish_interaction(
            "interaction.resolved",
            InteractionResolvedPayload(
                interaction_id=interaction_id,
                kind=resolved_view.kind,
                resolution_summary=summary,
                resolved_by=resolved_by,
            ),
            resolved_view,
        )
        return resolved_view

    def cancel_for_scope(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        conversation_id: str | None = None,
        reason: Literal["run_cancelled", "timed_out", "shutdown"] = "run_cancelled",
    ) -> int:
        """清理指定作用域下所有挂起交互，防止中断/退出后协程挂死或僵尸弹窗。"""
        matched_ids = [
            iid
            for iid, entry in self._pending.items()
            if (run_id is None or entry.view.run_id == run_id)
            and (task_id is None or entry.view.task_id == task_id)
            and (conversation_id is None or entry.view.conversation_id == conversation_id)
        ]

        for iid in matched_ids:
            entry = self._pending.pop(iid)
            self._resolved_ids.add(iid)
            if not entry.future.done():
                entry.future.set_result(self._fallback_for_cancel(entry.view.kind))
            self._publish_interaction(
                "interaction.cancelled",
                InteractionCancelledPayload(
                    interaction_id=iid, kind=entry.view.kind, reason=reason
                ),
                entry.view,
            )
        return len(matched_ids)

    def _convert_response(
        self,
        entry: _PendingInteraction,
        payload: InteractionRespondRequest,
    ) -> tuple[object, str]:
        if isinstance(payload, ApprovalRespondPayload):
            item_map = {
                item_id: ItemApprovalDecision(
                    approved=dec.approved,
                    grant_category_for_run=dec.grant_category_for_run,
                    reason=dec.reason,
                )
                for item_id, dec in payload.item_decisions.items()
            }
            if not item_map and payload.approved is None:
                raise InteractionPayloadError(
                    "Approval response must provide item_decisions or approved."
                )
            approved_flag = bool(payload.approved) if payload.approved is not None else any(
                d.approved for d in item_map.values()
            )
            decision = ApprovalDecision(
                approved=approved_flag,
                grant_categories_for_run=tuple(payload.grant_categories_for_run),
                reason=payload.reason,
                item_decisions=item_map,
            )
            if item_map:
                ok_cnt = sum(1 for d in item_map.values() if d.approved)
                summary = f"逐条审批完成: {ok_cnt}/{len(entry.approval_items)} 项放行"
            else:
                summary = "整批放行" if decision.approved else "整批拒绝"
            return decision, summary

        if isinstance(payload, AskUserRespondPayload):
            if payload.cancelled:
                return AskUserResponse(cancelled=True), "用户取消作答"
            converted: list[AskUserAnswer] = []
            for ans in payload.answers:
                note_parts: list[TextContent] = []
                note_lines: list[str] = []
                if ans.note_text.strip():
                    note_lines.append(ans.note_text.strip())
                for att_id in ans.attachment_ids:
                    ref = (
                        self._attachments.get_attachment(att_id)
                        if self._attachments is not None
                        else None
                    )
                    handle = (
                        ref.to_reference_handle()
                        if ref is not None
                        else f"[[attachment:{att_id}]]"
                    )
                    note_lines.append(handle)
                if note_lines:
                    note_parts.append(TextContent(content="\n".join(note_lines)))
                converted.append(
                    AskUserAnswer(
                        header=ans.header,
                        selected=tuple(ans.selected),
                        custom_answer=ans.custom_answer,
                        note=tuple(note_parts),
                    )
                )
            resp = AskUserResponse(answers=tuple(converted), cancelled=False)
            if entry.ask_user_request is not None:
                try:
                    check_response(entry.ask_user_request, resp)
                except ValueError as exc:
                    raise InteractionPayloadError(str(exc)) from exc
            return resp, f"已提交 {len(converted)} 项选择建议回答"

        assert isinstance(payload, McpElicitationRespondPayload)
        elicit_res = ElicitResult[dict[str, object]](
            action=payload.action,
            content=dict(payload.content) if payload.action == "accept" else None,
        )
        return elicit_res, f"MCP Elicitation: {payload.action}"

    @staticmethod
    def _fallback_for_cancel(kind: Literal["approval", "ask_user", "mcp_elicitation"]) -> object:
        if kind == "approval":
            return ApprovalDecision(approved=False, reason="交互因任务取消或关闭而终止")
        if kind == "ask_user":
            return AskUserResponse(cancelled=True)
        return ElicitResult[dict[str, object]](action="cancel", content=None)
