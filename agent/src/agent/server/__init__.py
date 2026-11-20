"""Layer 2 HTTP/SSE 传输服务包。"""

from agent.server.app import build_openapi_document, create_app, export_openapi_file
from agent.server.auth import ProtocolAuthMiddleware, is_trusted_local_request
from agent.server.lifecycle import (
    ParentPidWatchdog,
    RuntimeAlreadyRunningError,
    RuntimeDiscoveryInfo,
    SingleInstanceLock,
    build_discovery_info,
    cleanup_runtime_files,
    prepare_uds_path,
    write_runtime_discovery,
)

__all__ = [
    "ParentPidWatchdog",
    "ProtocolAuthMiddleware",
    "RuntimeAlreadyRunningError",
    "RuntimeDiscoveryInfo",
    "SingleInstanceLock",
    "build_discovery_info",
    "build_openapi_document",
    "cleanup_runtime_files",
    "create_app",
    "export_openapi_file",
    "is_trusted_local_request",
    "prepare_uds_path",
    "write_runtime_discovery",
]
