"""Layer 2 一机一实例进程互斥锁、发现文件 (0600) 原子管理与 Sidecar 父进程守护。"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent.protocol import PROTOCOL_VERSION, utc_rfc3339


class RuntimeAlreadyRunningError(RuntimeError):
    """本机已存在活跃的 Agent Runtime 实例。"""


@dataclass(frozen=True, kw_only=True)
class RuntimeDiscoveryInfo:
    """写入 ~/.agent/runtime.json 的单实例发现信息。"""

    pid: int
    uds_path: str | None
    http_url: str | None
    auth_token: str
    protocol_version: str = PROTOCOL_VERSION
    started_at: str
    db_path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "uds_path": self.uds_path,
            "http_url": self.http_url,
            "auth_token": self.auth_token,
            "protocol_version": self.protocol_version,
            "started_at": self.started_at,
            "db_path": self.db_path,
        }


class SingleInstanceLock:
    """基于 fcntl.flock 的单实例文件锁（崩溃后 OS 自动释放，绝不误删在运行实例文件）。"""

    def __init__(self, runtime_dir: str | Path) -> None:
        self._dir: Path = Path(runtime_dir).expanduser().resolve()
        self._lock_path: Path = self._dir / "runtime.lock"
        self._fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def acquire(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeAlreadyRunningError(
                f"Another agent runtime instance already holds {self._lock_path}"
            ) from exc
        _ = os.ftruncate(fd, 0)
        _ = os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


def prepare_uds_path(uds_path: str | Path) -> Path:
    """清理前次崩溃遗留的陈旧 socket 文件（仅在已持有 SingleInstanceLock 后调用）。"""
    path = Path(uds_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    return path


def write_runtime_discovery(runtime_dir: str | Path, info: RuntimeDiscoveryInfo) -> Path:
    """以 0600 权限原子写入 runtime.json 发现文件。"""
    directory = Path(runtime_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "runtime.json"
    tmp = directory / f"runtime.{uuid4().hex[:8]}.tmp"
    payload = json.dumps(info.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        _ = os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    _ = tmp.replace(target)
    return target


def cleanup_runtime_files(
    runtime_dir: str | Path, *, uds_path: str | Path | None = None
) -> None:
    """优雅退出时清理 runtime.json 与 runtime.sock。"""
    directory = Path(runtime_dir).expanduser().resolve()
    disc = directory / "runtime.json"
    with contextlib.suppress(OSError):
        if disc.exists():
            disc.unlink()
    if uds_path is not None:
        sock = Path(uds_path).expanduser().resolve()
        with contextlib.suppress(OSError):
            if sock.exists() or sock.is_symlink():
                sock.unlink()


class ParentPidWatchdog:
    """Sidecar 模式父进程存活守卫：父进程退出时触发优雅停机回调。"""

    def __init__(
        self,
        parent_pid: int,
        *,
        on_parent_exit: Callable[[], Awaitable[None]],
        poll_interval_sec: float = 1.0,
    ) -> None:
        self._parent_pid: int = parent_pid
        self._on_exit: Callable[[], Awaitable[None]] = on_parent_exit
        self._interval: float = max(0.1, poll_interval_sec)
        self._task: asyncio.Task[None] | None = None
        self._stopped: bool = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="parent-pid-watchdog")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            _ = self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self._interval)
            if not self._is_parent_alive():
                await self._on_exit()
                return

    def _is_parent_alive(self) -> bool:
        if self._parent_pid <= 1:
            return False
        if os.getppid() == 1 and self._parent_pid != 1:
            return False
        try:
            os.kill(self._parent_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def build_discovery_info(
    *,
    uds_path: Path | None,
    http_url: str | None,
    auth_token: str,
    db_path: str = "",
) -> RuntimeDiscoveryInfo:
    return RuntimeDiscoveryInfo(
        pid=os.getpid(),
        uds_path=str(uds_path) if uds_path is not None else None,
        http_url=http_url,
        auth_token=auth_token,
        started_at=utc_rfc3339(),
        db_path=db_path,
    )
