"""Agent CLI 入口: 支持 MCP OAuth 初次交互授权与终端 Elicitation 交互模式。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastmcp.client.elicitation import ElicitResult

from agent.core.runtime import open_runtime
from agent.extensions import (
    build_mcp_toolset,
    load_mcp_configs,
    run_interactive_oauth,
)


async def cli_elicitation_handler(
    message: str,
    response_type: type[Any] | None,
    params: object,
    context: object,
) -> ElicitResult[dict[str, str]] | dict[str, str]:
    """CLI 交互模式下的终端 Elicitation 处理器（后台 Worker 默认传 None 不启用）。"""
    _ = (response_type, params, context)
    print(f"\n[MCP Elicitation] {message}")
    answer = await asyncio.to_thread(input, "请输入确认/回复（直接回车取消）: ")
    if not answer.strip():
        return ElicitResult(action="cancel")
    return {"value": answer.strip()}


async def _run_mcp_auth(server_name: str, *, work_path: Path | None = None) -> int:
    merged = load_mcp_configs(workspace_root=work_path)
    target = next((s for s in merged.servers if s.name == server_name), None)
    if target is None:
        print(f"未找到 MCP server: {server_name!r}")
        return 1
    if target.auth.type == "oauth":
        await run_interactive_oauth(target)
    toolset = build_mcp_toolset(target)
    async with toolset:
        tools = await toolset.list_tools()
    print(f"MCP server {server_name!r} 授权成功，共发现 {len(tools)} 个工具。")
    return 0


async def _run_ask(prompt: str, *, work_path: Path | None = None) -> int:
    handler = cli_elicitation_handler if sys.stdin.isatty() else None
    async with open_runtime(
        mcp_workspace_root=work_path,
        elicitation_handler=handler,
    ) as runtime:
        reply = await runtime.router.handle_user_message(prompt)
        print(reply.output)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    """CLI 主入口。"""
    parser = argparse.ArgumentParser(prog="agent", description="Super Agent CLI")
    sub = parser.add_subparsers(dest="command")

    mcp_parser = sub.add_parser("mcp", help="MCP 管理命令")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_cmd")
    auth_parser = mcp_sub.add_parser("auth", help="执行 OAuth 交互授权并持久化 Token")
    _ = auth_parser.add_argument("server", help="mcp.json 中的 server 名称")
    _ = auth_parser.add_argument("--work-path", type=Path, default=None)

    ask_parser = sub.add_parser("ask", help="在 CLI 交互模式下运行一次请求")
    _ = ask_parser.add_argument("prompt", help="用户请求内容")
    _ = ask_parser.add_argument("--work-path", type=Path, default=None)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "mcp" and args.mcp_cmd == "auth":
        raise SystemExit(asyncio.run(_run_mcp_auth(args.server, work_path=args.work_path)))
    if args.command == "ask":
        raise SystemExit(asyncio.run(_run_ask(args.prompt, work_path=args.work_path)))
    parser.print_help()
