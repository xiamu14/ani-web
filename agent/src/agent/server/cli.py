"""Layer 2 CLI 服务入口：`agent serve`（支持 UDS + Localhost HTTP 双监听与 Sidecar 守护）。"""

from __future__ import annotations

import argparse
import asyncio
import secrets
from pathlib import Path
from typing import cast

import uvicorn

from agent.core import open_runtime
from agent.db import default_db_path
from agent.server.app import create_app
from agent.server.lifecycle import (
    ParentPidWatchdog,
    SingleInstanceLock,
    build_discovery_info,
    cleanup_runtime_files,
    prepare_uds_path,
    write_runtime_discovery,
)


async def serve_async(
    *,
    runtime_dir: Path,
    uds_path: Path | None,
    host: str,
    port: int,
    parent_pid: int | None = None,
    auth_token: str | None = None,
) -> None:
    """启动一机一实例双传输协议服务器。"""
    lock = SingleInstanceLock(runtime_dir)
    lock.acquire()
    effective_uds = prepare_uds_path(uds_path) if uds_path is not None else None
    effective_token = auth_token or secrets.token_urlsafe(32)
    http_url = f"http://{host}:{port}" if port > 0 else None

    try:
        async with open_runtime(enable_protocol_services=True) as runtime:
            app = create_app(runtime, auth_token=effective_token)
            _ = write_runtime_discovery(
                runtime_dir,
                build_discovery_info(
                    uds_path=effective_uds,
                    http_url=http_url,
                    auth_token=effective_token,
                    db_path=str(default_db_path()),
                ),
            )
            servers: list[uvicorn.Server] = []
            if effective_uds is not None:
                cfg_uds = uvicorn.Config(
                    app, uds=str(effective_uds), log_level="warning"
                )
                servers.append(uvicorn.Server(cfg_uds))
            if port > 0:
                cfg_http = uvicorn.Config(
                    app, host=host, port=port, log_level="warning"
                )
                servers.append(uvicorn.Server(cfg_http))

            async def _shutdown_all() -> None:
                for srv in servers:
                    srv.should_exit = True

            watchdog = (
                ParentPidWatchdog(parent_pid, on_parent_exit=_shutdown_all)
                if parent_pid is not None
                else None
            )
            if watchdog is not None:
                watchdog.start()
            try:
                _ = await asyncio.gather(*(srv.serve() for srv in servers))
            finally:
                if watchdog is not None:
                    await watchdog.stop()
    finally:
        cleanup_runtime_files(runtime_dir, uds_path=effective_uds)
        lock.release()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent", description="Super-Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_p = sub.add_parser("serve", help="Start GUI protocol server")
    _ = serve_p.add_argument("--runtime-dir", default="~/.agent")
    _ = serve_p.add_argument("--uds", default="~/.agent/runtime.sock")
    _ = serve_p.add_argument("--no-uds", action="store_true")
    _ = serve_p.add_argument("--host", default="127.0.0.1")
    _ = serve_p.add_argument("--port", type=int, default=8765)
    _ = serve_p.add_argument("--parent-pid", type=int, default=None)
    args = parser.parse_args(argv)

    ns = cast(dict[str, object], vars(args))
    if ns.get("command") == "serve":
        rdir = Path(str(ns["runtime_dir"])).expanduser().resolve()
        uds = None if bool(ns.get("no_uds")) else Path(str(ns["uds"])).expanduser().resolve()
        raw_ppid = ns.get("parent_pid")
        ppid = int(str(raw_ppid)) if raw_ppid is not None else None
        asyncio.run(
            serve_async(
                runtime_dir=rdir,
                uds_path=uds,
                host=str(ns["host"]),
                port=int(str(ns["port"])),
                parent_pid=ppid,
            )
        )


if __name__ == "__main__":
    main()
