"""权限审核决策层: Jev 语义意图复核、同轮批量聚合确认与单次 run_id 临时授权。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

import httpx
import logfire

from agent.llm import JevHttpClient
from agent.permissions.rules import McpRuleInfo, RuleVerdict, evaluate_tool_rule

JevRiskVerdict = Literal["ALLOW", "CONFIRM_USER", "DENY"]
_VALID_VERDICTS: frozenset[str] = frozenset({"ALLOW", "CONFIRM_USER", "DENY"})


@dataclass(frozen=True, kw_only=True)
class ApprovalItem:
    """待用户批量确认的单个工具调用描述。"""

    item_id: str = field(default_factory=lambda: uuid4().hex[:12])
    tool_name: str
    args: Mapping[str, object]
    category: str
    rule_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class ApprovalContext:
    """权限审核请求所属主会话、任务与运行上下文。"""

    main_conversation_id: str = ""
    task_id: str | None = None
    role_name: str | None = None
    run_id: str = ""
    user_intent: str = ""


@dataclass(frozen=True, kw_only=True)
class ItemApprovalDecision:
    """单条工具调用的逐条勾选裁决。"""

    approved: bool
    grant_category_for_run: bool = False
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class ApprovalDecision:
    """用户批量确认结果（支持逐条 item_decisions 裁决与整批兼容回退）。"""

    approved: bool = False
    grant_categories_for_run: tuple[str, ...] = ()
    reason: str = ""
    item_decisions: Mapping[str, ItemApprovalDecision] = field(
        default_factory=lambda: dict[str, ItemApprovalDecision]()
    )


@dataclass(frozen=True, kw_only=True)
class AuthorizationResult:
    """权限审核最终裁决。"""

    allowed: bool
    reason: str = ""
    decision_source: str = ""


class UserApprovalPort(Protocol):
    """用户异步确认端口（与 CLI/TUI/Web 实现完全解耦）。"""

    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision: ...


class ContextualUserApprovalPort(UserApprovalPort, Protocol):
    """支持透传 ApprovalContext 的扩展用户确认端口。"""

    async def request_batch_approval_with_context(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
        context: ApprovalContext | None = None,
    ) -> ApprovalDecision: ...


@dataclass
class _PendingApproval:
    item: ApprovalItem
    user_intent: str
    context: ApprovalContext | None
    future: asyncio.Future[tuple[bool, str]]


class PermissionReviewer:
    """结合静态规则、Jev 语义意图判定与同轮批量确认的权限审核器。"""

    def __init__(
        self,
        *,
        jev_client: JevHttpClient | None = None,
        approval_port: UserApprovalPort | None = None,
        batch_window_seconds: float = 0.015,
        mcp_policies: Mapping[str, McpRuleInfo] | None = None,
    ) -> None:
        self._jev: JevHttpClient | None = jev_client
        self._approval: UserApprovalPort | None = approval_port
        self._batch_window: float = batch_window_seconds
        self._run_grants: dict[str, set[str]] = {}
        self._pending: dict[str, list[_PendingApproval]] = {}
        self._batch_locks: dict[str, asyncio.Lock] = {}
        self._mcp_policies: dict[str, McpRuleInfo] = dict(mcp_policies or {})

    def set_mcp_policies(self, mcp_policies: Mapping[str, McpRuleInfo]) -> None:
        """原子替换运行期 MCP 预编译策略索引（key 为运行期工具名）。"""
        self._mcp_policies = dict(mcp_policies)

    def clear_run(self, run_id: str) -> None:
        """单次 Agent Run 结束后销毁其临时同类授权，确保不演变为永久白名单。"""
        _ = self._run_grants.pop(run_id, None)
        _ = self._pending.pop(run_id, None)
        _ = self._batch_locks.pop(run_id, None)

    async def authorize(
        self,
        *,
        run_id: str,
        tool_name: str,
        args: Mapping[str, object],
        workspace_root: str | Path | None = None,
        user_intent: str = "",
        is_mcp_tool: bool = False,
        runtime_schema_hash: str | None = None,
        context: ApprovalContext | None = None,
    ) -> AuthorizationResult:
        """执行三级权限裁决: 静态 Rules -> (REVIEW 时) Jev 语义 -> 批量用户确认。"""
        mcp_policy = self._mcp_policies.get(tool_name)
        verdict: RuleVerdict = evaluate_tool_rule(
            tool_name,
            args,
            workspace_root=workspace_root,
            mcp_policy=mcp_policy,
            is_mcp_tool=is_mcp_tool,
            runtime_schema_hash=runtime_schema_hash,
        )
        if verdict.level == "forbid":
            return AuthorizationResult(
                allowed=False,
                reason=verdict.reason,
                decision_source=f"rule:{verdict.rule_id or 'forbid'}",
            )
        if verdict.level == "pass":
            return AuthorizationResult(allowed=True, decision_source="rule:pass")

        if verdict.category and verdict.category in self._run_grants.get(run_id, set()):
            return AuthorizationResult(
                allowed=True,
                reason=f"当前运行 {run_id} 已获同类授权: {verdict.category}",
                decision_source="run_grant",
            )

        jev_choice: JevRiskVerdict = "CONFIRM_USER"
        if self._jev is not None:
            jev_choice = await self._review_with_jev(
                run_id=run_id,
                tool_name=tool_name,
                args=args,
                workspace_root=workspace_root,
                user_intent=user_intent,
                verdict=verdict,
            )

        if jev_choice == "ALLOW":
            return AuthorizationResult(
                allowed=True,
                reason="Jev 判定该操作已获用户明确指令授权",
                decision_source="jev:allow",
            )
        if jev_choice == "DENY":
            return AuthorizationResult(
                allowed=False,
                reason=f"Jev 风险审核拒绝: {verdict.reason}",
                decision_source="jev:deny",
            )

        item = ApprovalItem(
            tool_name=tool_name,
            args=dict(args),
            category=verdict.category or "general_review",
            rule_id=verdict.rule_id or "review",
            reason=verdict.reason,
        )
        approved, reason = await self._coalesced_user_approval(
            run_id, item, user_intent, context=context
        )
        return AuthorizationResult(
            allowed=approved,
            reason=reason,
            decision_source="user:approved" if approved else "user:denied",
        )

    async def _review_with_jev(
        self,
        *,
        run_id: str,
        tool_name: str,
        args: Mapping[str, object],
        workspace_root: str | Path | None,
        user_intent: str,
        verdict: RuleVerdict,
    ) -> JevRiskVerdict:
        assert self._jev is not None
        state: dict[str, object] = {
            "user_intent": user_intent or "(未提供)",
            "workspace_root": str(workspace_root) if workspace_root is not None else "(无)",
            "tool_name": tool_name,
            "tool_args": dict(args),
            "matched_rule": {
                "rule_id": verdict.rule_id or "",
                "category": verdict.category,
                "reason": verdict.reason,
            },
        }
        questions: dict[str, object] = {
            "verdict": {
                "type": "choice",
                "instructions": (
                    "Evaluate whether the tool call should be allowed, confirmed by the user, or denied. "
                    "Choose ALLOW if user_intent explicitly requested this exact action/package/target within safe bounds; "
                    "choose CONFIRM_USER if the user only stated a high-level goal (e.g. initialize a project) and the agent "
                    "implicitly triggered package installation, network access, or critical config edits; "
                    "choose DENY if it is out-of-bounds or destructive."
                ),
                "criteria": {
                    "ALLOW": "Explicitly authorized by user_intent and safe within workspace scope.",
                    "CONFIRM_USER": "Implicitly triggered by the agent under a high-level goal; risky but not destructive, needs user confirmation.",
                    "DENY": "Out-of-bounds, destructive, or inconsistent with user_intent.",
                },
            }
        }
        try:
            data = await self._jev.request_decisions(
                state=state,
                questions=questions,
                span_name="jev.permission_review {model}",
                span_attrs={
                    "run_id": run_id,
                    "tool_name": tool_name,
                    "rule_id": verdict.rule_id or "",
                    "category": verdict.category,
                },
            )
        except (httpx.HTTPError, ValueError) as exc:
            logfire.warn(
                "Jev permission review unavailable, failing closed to CONFIRM_USER",
                run_id=run_id,
                tool_name=tool_name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return "CONFIRM_USER"
        answers = cast(dict[str, dict[str, object]], data.get("answers", {}))
        raw_choice = str(answers.get("verdict", {}).get("choice", "CONFIRM_USER"))
        return cast(JevRiskVerdict, raw_choice) if raw_choice in _VALID_VERDICTS else "CONFIRM_USER"

    async def _coalesced_user_approval(
        self,
        run_id: str,
        item: ApprovalItem,
        user_intent: str,
        *,
        context: ApprovalContext | None = None,
    ) -> tuple[bool, str]:
        if self._approval is None:
            return False, f"未配置用户确认处理器，拒绝待审核操作: {item.reason}"

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[bool, str]] = loop.create_future()
        queue = self._pending.setdefault(run_id, [])
        is_leader = len(queue) == 0
        queue.append(
            _PendingApproval(
                item=item, user_intent=user_intent, context=context, future=fut
            )
        )

        if is_leader:
            if self._batch_window > 0:
                await asyncio.sleep(self._batch_window)
            lock = self._batch_locks.setdefault(run_id, asyncio.Lock())
            async with lock:
                batch = self._pending.pop(run_id, [])
                if batch:
                    await self._resolve_batch(run_id, batch)

        return await fut

    async def _resolve_batch(self, run_id: str, batch: list[_PendingApproval]) -> None:
        assert self._approval is not None
        items = [entry.item for entry in batch]
        intent = next((entry.user_intent for entry in batch if entry.user_intent), "")
        batch_ctx = next((entry.context for entry in batch if entry.context is not None), None)
        if batch_ctx is None:
            batch_ctx = ApprovalContext(run_id=run_id, user_intent=intent)
        with logfire.span(
            "permission.user_approval_batch",
            run_id=run_id,
            item_count=len(items),
            tools=[i.tool_name for i in items],
        ) as span:
            ctx_fn = getattr(self._approval, "request_batch_approval_with_context", None)
            if callable(ctx_fn):
                decision = await cast(ContextualUserApprovalPort, self._approval).request_batch_approval_with_context(
                    items, user_intent=intent, context=batch_ctx
                )
            else:
                decision = await self._approval.request_batch_approval(
                    items, user_intent=intent
                )
            span.set_attribute("approved", decision.approved)
            span.set_attribute("item_decisions_count", len(decision.item_decisions))
            span.set_attribute("grant_categories_for_run", list(decision.grant_categories_for_run))

        if decision.item_decisions:
            for entry in batch:
                item_dec = decision.item_decisions.get(entry.item.item_id)
                if item_dec is None:
                    # 逐条审批模式中未显式裁决的条目严格遵循 fail-closed 默认拒绝
                    if not entry.future.done():
                        entry.future.set_result(
                            (False, "未在逐条审批结果中确认该操作（默认拒绝）")
                        )
                    continue
                if item_dec.approved:
                    if item_dec.grant_category_for_run and entry.item.category:
                        self._run_grants.setdefault(run_id, set()).add(
                            entry.item.category
                        )
                    msg = item_dec.reason or decision.reason or "用户已确认放行"
                else:
                    msg = item_dec.reason or decision.reason or "用户拒绝放行待审核操作"
                if not entry.future.done():
                    entry.future.set_result((item_dec.approved, msg))
            return

        if decision.approved:
            if decision.grant_categories_for_run:
                self._run_grants.setdefault(run_id, set()).update(decision.grant_categories_for_run)
            msg = decision.reason or "用户已确认放行"
        else:
            msg = decision.reason or "用户拒绝放行待审核操作"

        for entry in batch:
            if not entry.future.done():
                entry.future.set_result((decision.approved, msg))
