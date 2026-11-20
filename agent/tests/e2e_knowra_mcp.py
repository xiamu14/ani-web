"""真实 Knowra MCP OAuth 授权 + 真实 LLM 调用 + MCP HTTP 报文日志采集 E2E。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from agent.core.runtime import open_runtime
from agent.extensions import (
    LocalOAuthReadinessChecker,
    build_mcp_toolset,
    load_mcp_configs,
)
from agent.permissions import ApprovalDecision, ApprovalItem

LOG_PATH = Path("logs/knowra_mcp_http.jsonl")


class _AutoApprovePort:
    async def request_batch_approval(
        self,
        items: Sequence[ApprovalItem],
        *,
        user_intent: str = "",
    ) -> ApprovalDecision:
        _ = (items, user_intent)
        return ApprovalDecision(approved=True, reason="e2e-auto-approved")


def _make_logging_http_client(log_path: Path) -> httpx2.AsyncClient:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    async def _on_request(request: httpx2.Request) -> None:
        raw_body = request.content.decode("utf-8", errors="replace") if request.content else ""
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "direction": "request",
            "method": request.method,
            "url": str(request.url),
            "bytes": len(request.content or b""),
            "approx_tokens": max(1, len(raw_body) // 4) if raw_body else 0,
            "body": raw_body[:8000],
        }
        with log_path.open("a", encoding="utf-8") as fh:
            _ = fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def _on_response(response: httpx2.Response) -> None:
        body_bytes = await response.aread()
        body_text = body_bytes.decode("utf-8", errors="replace")
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "direction": "response",
            "method": response.request.method,
            "url": str(response.request.url),
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "bytes": len(body_bytes),
            "approx_tokens": max(1, len(body_text) // 4) if body_text else 0,
            "body": body_text[:16000],
        }
        with log_path.open("a", encoding="utf-8") as fh:
            _ = fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return httpx2.AsyncClient(
        event_hooks={"request": [_on_request], "response": [_on_response]},
        timeout=60.0,
    )


async def main() -> None:
    ws = Path.cwd()
    merged = load_mcp_configs(workspace_root=ws)
    knowra = next((s for s in merged.servers if s.name == "knowra"), None)
    if knowra is None:
        raise RuntimeError("未在 .agent/mcp.json 找到 knowra 配置")

    if LOG_PATH.exists():
        LOG_PATH.unlink()

    http_client = _make_logging_http_client(LOG_PATH)
    try:
        checker = LocalOAuthReadinessChecker()
        if not await checker.is_ready(knowra):
            print("Knowra 尚未授权，正在启动 OAuth 2.1 浏览器授权流程，请在浏览器中完成登录...")
            toolset = build_mcp_toolset(knowra, http_client=http_client)
            async with toolset:
                tools = await toolset.list_tools()
            print(f"OAuth 授权成功并已持久化！共发现 {len(tools)} 个 Knowra 工具: {[t.name for t in tools]}")
        else:
            print("检测到本地已缓存 Knowra OAuth Token，跳过浏览器登录。")

        e2e_db = Path("logs/e2e_knowra_ctx.db")
        e2e_steps = Path("logs/e2e_knowra_steps.db")
        for p in (e2e_db, e2e_steps):
            if p.exists():
                p.unlink()

        async with open_runtime(
            db_path=e2e_db,
            step_db_path=e2e_steps,
            mcp_workspace_root=ws,
            approval_port=_AutoApprovePort(),
            mcp_http_client=http_client,
        ) as runtime:
            report = runtime.mcp_compile_report
            print("MCP 预编译结果:", report)

            main_conv = await runtime.context_tree.ensure_active_conversation()
            receipt = await runtime.scheduler.dispatch_task(
                main_conversation_id=main_conv.conversation_id,
                role_name="researcher",
                mandate=(
                    "请调用 knowra MCP 工具：先调用 knowra_get_usage 查看当前配额，"
                    "再调用 knowra_search_papers 搜索关键词 'agent memory'，并简要总结返回内容。"
                ),
                work_path=str(ws),
            )
            await runtime.scheduler.wait_all()
            ticket = await runtime.ticket_store.get_ticket(main_conv.conversation_id, receipt.task_id)
            print("Role 任务状态:", ticket.status if ticket else None)
            print("Role 输出:\n", ticket.result_summary if ticket else None)
    finally:
        await http_client.aclose()

    if LOG_PATH.exists():
        lines = [json.loads(line) for line in LOG_PATH.read_text(encoding="utf-8").splitlines() if line]
        resp_bytes = sum(int(item["bytes"]) for item in lines if item["direction"] == "response")
        resp_tokens = sum(int(item["approx_tokens"]) for item in lines if item["direction"] == "response")
        print(f"\n=== HTTP 日志统计 ({LOG_PATH}) ===")
        print(f"总事件数: {len(lines)}, 响应总字节: {resp_bytes}, 估算响应 Token: ~{resp_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
