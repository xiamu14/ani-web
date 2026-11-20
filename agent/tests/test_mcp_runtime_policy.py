"""Step 6.4 单测: MCP 运行期 O(1) 策略查表、未登记/漂移兜底与 ToolLockCoordinator 并发/串行协调。"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from agent.hooks import PermissionGuardHook
from agent.permissions import (
    ApprovalDecision,
    ApprovalItem,
    McpToolPolicy,
    PermissionReviewer,
    evaluate_tool_rule,
)
from agent.tools import (
    ToolExecutionQueueHook,
    ToolLockCoordinator,
    resolve_tool_resource,
)


class _RecordingApprovalPort:
    def __init__(self, *, approved: bool = True) -> None:
        self.approved: bool = approved
        self.calls: list[tuple[str, ...]] = []

    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision:
        _ = user_intent
        self.calls.append(tuple(i.tool_name for i in items))
        return ApprovalDecision(approved=self.approved, reason="recorded")


def _dummy_ctx() -> RunContext[object]:
    return RunContext(deps=object(), model=TestModel(), usage=RunUsage(), run_id="run-mcp-1")


def _default_policies() -> dict[str, McpToolPolicy]:
    return {
        "docs_search": McpToolPolicy(
            server_name="docs",
            endpoint_fingerprint="fp-docs",
            server_impl_name="docs-mcp",
            server_version="1.0.0",
            tool_name="search",
            prefixed_tool_name="docs_search",
            schema_hash="hash-search-v1",
            level="pass",
            lock_mode="shared",
            recommended_roles=("researcher", "coder"),
            reason="read-only search",
        ),
        "db_drop": McpToolPolicy(
            server_name="db",
            endpoint_fingerprint="fp-db",
            server_impl_name="db-mcp",
            server_version="1.0.0",
            tool_name="drop",
            prefixed_tool_name="db_drop",
            schema_hash="hash-drop-v1",
            level="forbid",
            lock_mode="exclusive",
            recommended_roles=(),
            reason="destructive drop",
        ),
        "git_commit": McpToolPolicy(
            server_name="git",
            endpoint_fingerprint="fp-git",
            server_impl_name="git-mcp",
            server_version="1.0.0",
            tool_name="commit",
            prefixed_tool_name="git_commit",
            schema_hash="hash-commit-v1",
            level="review",
            lock_mode="exclusive",
            recommended_roles=("coder",),
            reason="mutating git repo",
        ),
    }


class TestMcpRuntimePolicy(unittest.IsolatedAsyncioTestCase):
    def test_o1_rule_lookup_and_fail_safe_fallbacks(self) -> None:
        policies = _default_policies()
        pass_verdict = evaluate_tool_rule("docs_search", {}, mcp_policy=policies["docs_search"])
        self.assertEqual(pass_verdict.level, "pass")

        forbid_verdict = evaluate_tool_rule("db_drop", {}, mcp_policy=policies["db_drop"])
        self.assertEqual(forbid_verdict.level, "forbid")

        unregistered = evaluate_tool_rule("docs_unknown", {}, mcp_policy=None, is_mcp_tool=True)
        self.assertEqual(unregistered.level, "review")
        self.assertEqual(unregistered.rule_id, "review.mcp.unregistered")

        drifted = evaluate_tool_rule(
            "docs_search",
            {},
            mcp_policy=policies["docs_search"],
            runtime_schema_hash="hash-search-v2",
        )
        self.assertEqual(drifted.level, "review")
        self.assertEqual(drifted.rule_id, "review.mcp.schema_drift")

    async def test_permission_guard_hook_forbid_pass_review_and_drift(self) -> None:
        policies = _default_policies()
        approval_port = _RecordingApprovalPort(approved=True)
        reviewer = PermissionReviewer(approval_port=approval_port, mcp_policies=policies)
        hook = PermissionGuardHook(reviewer=reviewer, known_mcp_prefixes=("docs", "db", "git"))
        ctx = _dummy_ctx()

        # 1. pass -> 零审批直接放行
        args = await hook.before_tool_execute(
            ctx,
            call=ToolCallPart(tool_name="docs_search", args={"q": "mcp"}),
            tool_def=ToolDefinition(name="docs_search", description="search"),
            args={"q": "mcp"},
        )
        self.assertEqual(args, {"q": "mcp"})
        self.assertEqual(approval_port.calls, [])

        # 2. forbid -> 零审批直接抛出 ModelRetry
        with self.assertRaises(ModelRetry):
            _ = await hook.before_tool_execute(
                ctx,
                call=ToolCallPart(tool_name="db_drop", args={}),
                tool_def=ToolDefinition(name="db_drop", description="drop"),
                args={},
            )
        self.assertEqual(approval_port.calls, [])

        # 3. review -> 走 PermissionReviewer 审批
        _ = await hook.before_tool_execute(
            ctx,
            call=ToolCallPart(tool_name="git_commit", args={"msg": "init"}),
            tool_def=ToolDefinition(name="git_commit", description="commit"),
            args={"msg": "init"},
        )
        self.assertEqual(approval_port.calls, [("git_commit",)])

        # 4. 未登记前缀 / Schema 漂移 -> 兜底走 review
        _ = await hook.before_tool_execute(
            ctx,
            call=ToolCallPart(tool_name="docs_new_tool", args={}),
            tool_def=ToolDefinition(name="docs_new_tool", description="new"),
            args={},
        )
        _ = await hook.before_tool_execute(
            ctx,
            call=ToolCallPart(tool_name="docs_search", args={}),
            tool_def=ToolDefinition(
                name="docs_search",
                description="search",
                metadata={"mcp_schema_hash": "drifted-hash"},
            ),
            args={},
        )
        self.assertEqual(
            approval_port.calls,
            [("git_commit",), ("docs_new_tool",), ("docs_search",)],
        )

    async def test_shared_concurrent_and_exclusive_serial_across_roles(self) -> None:
        coordinator = ToolLockCoordinator()
        shared_policy = _default_policies()["docs_search"]
        exclusive_policy = McpToolPolicy(
            server_name="docs",
            endpoint_fingerprint="fp-docs",
            server_impl_name="docs-mcp",
            server_version="1.0.0",
            tool_name="reindex",
            prefixed_tool_name="docs_reindex",
            schema_hash="hash-reindex-v1",
            level="review",
            lock_mode="exclusive",
            recommended_roles=("coder",),
        )
        hook_role_a = ToolExecutionQueueHook(
            coordinator=coordinator,
            mcp_policies={"docs_search": shared_policy, "docs_reindex": exclusive_policy},
            known_mcp_prefixes=("docs",),
        )
        hook_role_b = ToolExecutionQueueHook(
            coordinator=coordinator,
            mcp_policies={"docs_search": shared_policy, "docs_reindex": exclusive_policy},
            known_mcp_prefixes=("docs",),
        )

        # 未登记 MCP 工具资源解析兜底为 exclusive
        unreg_res = resolve_tool_resource("docs_unknown", {}, is_mcp_tool=True)
        self.assertEqual(unreg_res.lock_mode, "exclusive")

        # 1. 同 Server shared 只读操作跨 Role 并发
        active_shared = 0
        max_shared = 0

        async def _shared_handler(_args: dict[str, object]) -> str:
            nonlocal active_shared, max_shared
            active_shared += 1
            max_shared = max(max_shared, active_shared)
            await asyncio.sleep(0.03)
            active_shared -= 1
            return "ok"

        _ = await asyncio.gather(
            hook_role_a.wrap_tool_execute(
                _dummy_ctx(),
                call=ToolCallPart(tool_name="docs_search", args={}),
                tool_def=ToolDefinition(name="docs_search", description="s"),
                args={},
                handler=_shared_handler,
            ),
            hook_role_b.wrap_tool_execute(
                _dummy_ctx(),
                call=ToolCallPart(tool_name="docs_search", args={}),
                tool_def=ToolDefinition(name="docs_search", description="s"),
                args={},
                handler=_shared_handler,
            ),
        )
        self.assertEqual(max_shared, 2)

        # 2. 同 Server exclusive 写操作跨 Role 严格串行
        active_excl = 0
        max_excl = 0

        async def _excl_handler(_args: dict[str, object]) -> str:
            nonlocal active_excl, max_excl
            active_excl += 1
            max_excl = max(max_excl, active_excl)
            await asyncio.sleep(0.02)
            active_excl -= 1
            return "ok"

        _ = await asyncio.gather(
            hook_role_a.wrap_tool_execute(
                _dummy_ctx(),
                call=ToolCallPart(tool_name="docs_reindex", args={}),
                tool_def=ToolDefinition(name="docs_reindex", description="r"),
                args={},
                handler=_excl_handler,
            ),
            hook_role_b.wrap_tool_execute(
                _dummy_ctx(),
                call=ToolCallPart(tool_name="docs_reindex", args={}),
                tool_def=ToolDefinition(name="docs_reindex", description="r"),
                args={},
                handler=_excl_handler,
            ),
        )
        self.assertEqual(max_excl, 1)
