"""Step 12.5: Starlette HTTP/SSE Server、一机一实例锁、鉴权隔离与 Range 预览单测。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import cast

import httpx

from agent.core import open_runtime
from agent.server import (
    RuntimeAlreadyRunningError,
    SingleInstanceLock,
    build_discovery_info,
    cleanup_runtime_files,
    create_app,
    export_openapi_file,
    prepare_uds_path,
    write_runtime_discovery,
)


class TestStep125LifecycleAndServer(unittest.IsolatedAsyncioTestCase):
    """Step 12.5: 单实例互斥、0600 发现文件、Bearer/Origin 鉴权、UDS 隔离与 Range 切片。"""

    def test_single_instance_flock_and_0600_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rdir = Path(tmp) / "agent_rt"
            lock1 = SingleInstanceLock(rdir)
            lock1.acquire()
            try:
                uds = prepare_uds_path(rdir / "runtime.sock")
                info = build_discovery_info(
                    uds_path=uds,
                    http_url="http://127.0.0.1:8765",
                    auth_token="secret-token-123",
                    db_path=str(rdir / "context_tree.db"),
                )
                disc_path = write_runtime_discovery(rdir, info)
                self.assertTrue(disc_path.is_file())
                mode = stat.S_IMODE(os.stat(disc_path).st_mode)
                self.assertEqual(mode, 0o600)

                # 第 2 个实例试图拿锁必须立即失败，且不得破坏已有 runtime.json
                lock2 = SingleInstanceLock(rdir)
                with self.assertRaises(RuntimeAlreadyRunningError):
                    lock2.acquire()
                self.assertTrue(disc_path.is_file())
            finally:
                cleanup_runtime_files(rdir, uds_path=rdir / "runtime.sock")
                lock1.release()
            self.assertFalse((rdir / "runtime.json").exists())

    async def test_auth_origin_uds_isolation_range_and_rest_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sample_file = tmp_path / "hello.txt"
            _ = sample_file.write_bytes(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")

            async with open_runtime(
                db_path=tmp_path / "ctx.db",
                step_db_path=tmp_path / "step.db",
                media_dir=tmp_path / "media",
                enable_protocol_services=True,
            ) as rt:
                _ = await rt.context_tree.ensure_active_conversation(target_title="默认会话")
                app = create_app(rt, auth_token="tok-xyz")

                # 1. HTTP 客户端（非 UDS）
                http_transport = httpx.ASGITransport(
                    app=app, client=("127.0.0.1", 54321)
                )
                async with httpx.AsyncClient(
                    transport=http_transport, base_url="http://127.0.0.1:8765"
                ) as client:
                    # /v1/health 允许免 Token
                    r_health = await client.get("/v1/health")
                    self.assertEqual(r_health.status_code, 200)
                    health_body = cast(dict[str, object], r_health.json())
                    self.assertFalse(bool(health_body["trusted_local"]))

                    # 缺 Bearer -> 401
                    r_unauth = await client.get("/v1/runtime/snapshot")
                    self.assertEqual(r_unauth.status_code, 401)
                    self.assertEqual(cast(dict[str, object], r_unauth.json())["code"], "UNAUTHORIZED")

                    # 非法 Origin -> 403
                    r_origin = await client.get(
                        "/v1/runtime/snapshot",
                        headers={
                            "Authorization": "Bearer tok-xyz",
                            "Origin": "https://evil.example.com",
                        },
                    )
                    self.assertEqual(r_origin.status_code, 403)
                    self.assertEqual(cast(dict[str, object], r_origin.json())["code"], "FORBIDDEN_ORIGIN")

                    auth_headers = {"Authorization": "Bearer tok-xyz"}

                    # HTTP 禁止 /v1/attachments/local -> 403
                    r_local_denied = await client.post(
                        "/v1/attachments/local",
                        headers=auth_headers,
                        json={"file_path": str(sample_file)},
                    )
                    self.assertEqual(r_local_denied.status_code, 403)
                    self.assertEqual(
                        cast(dict[str, object], r_local_denied.json())["code"],
                        "UNTRUSTED_LOCAL_ENDPOINT",
                    )

                    # multipart 上传 + Range 预览
                    r_up = await client.post(
                        "/v1/attachments",
                        headers=auth_headers,
                        files={"file": ("hello.txt", sample_file.read_bytes(), "text/plain")},
                    )
                    self.assertEqual(r_up.status_code, 201)
                    up_body = cast(dict[str, dict[str, object]], r_up.json())
                    att_id = str(up_body["attachment"]["attachment_id"])

                    r_range = await client.get(
                        f"/v1/attachments/{att_id}/content",
                        headers={**auth_headers, "Range": "bytes=0-9"},
                    )
                    self.assertEqual(r_range.status_code, 206)
                    self.assertEqual(r_range.content, b"0123456789")
                    self.assertEqual(r_range.headers["content-range"], "bytes 0-9/36")

                    # Snapshot / Tree / Conversation CRUD / Usage
                    r_snap = await client.get("/v1/runtime/snapshot", headers=auth_headers)
                    self.assertEqual(r_snap.status_code, 200)
                    snap_body = cast(dict[str, dict[str, object]], r_snap.json())
                    active_cid = str(snap_body["tree"]["active_conversation_id"])

                    r_fork = await client.post(
                        f"/v1/conversations/{active_cid}/fork",
                        headers=auth_headers,
                        json={"mode": "full_fork", "target_title": "实验分支"},
                    )
                    self.assertEqual(r_fork.status_code, 201)
                    forked_cid = str(cast(dict[str, object], r_fork.json())["conversation_id"])

                    r_entries = await client.get(
                        f"/v1/conversations/{forked_cid}/entries", headers=auth_headers
                    )
                    self.assertEqual(r_entries.status_code, 200)

                    r_usage = await client.get(
                        f"/v1/conversations/{forked_cid}/usage", headers=auth_headers
                    )
                    self.assertEqual(r_usage.status_code, 200)
                    self.assertIn("main_router_usage", cast(dict[str, object], r_usage.json()))

                # 2. UDS 受信任客户端（client=("", 0)）允许 /v1/attachments/local 且免 Bearer
                uds_transport = httpx.ASGITransport(app=app, client=("", 0))
                async with httpx.AsyncClient(
                    transport=uds_transport, base_url="http://localhost"
                ) as uds_client:
                    r_local_ok = await uds_client.post(
                        "/v1/attachments/local",
                        json={"file_path": str(sample_file)},
                    )
                    self.assertEqual(r_local_ok.status_code, 201)
                    self.assertEqual(cast(dict[str, object], r_local_ok.json())["title"], "hello.txt")

                # 3. 导出 OpenAPI 文档校验
                oa_path = export_openapi_file(app, tmp_path / "openapi.json")
                self.assertTrue(oa_path.is_file())
                oa_doc = cast(dict[str, dict[str, object]], json.loads(oa_path.read_text(encoding="utf-8")))
                self.assertIn("/v1/events", oa_doc["paths"])
                self.assertIn("/v1/interactions/{interaction_id}/respond", oa_doc["paths"])
