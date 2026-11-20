"""Step 6.3 单测: mcp.json 合并、Auth/Token 隔离、脱敏指纹、SQLite mcp_tool_policies 与增量编译。"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from cryptography.fernet import Fernet
from fastmcp.client.auth import BearerAuth, ClientCredentialsOAuthProvider, OAuth

from agent.db import SQLModelMcpPolicyStore, create_sqlite_async_engine, init_async_db
from agent.extensions.mcp_auth import (
    LocalOAuthReadinessChecker,
    build_fastmcp_auth,
    build_token_storage,
    token_namespace_dir,
)
from agent.extensions.mcp_config import (
    McpServerSpec,
    compute_tool_schema_hash,
    load_mcp_configs,
    merge_mcp_configs,
)
from agent.extensions.mcp_loader import build_mcp_capability
from agent.permissions.mcp_compiler import McpPolicyCompiler
from agent.permissions.mcp_ports import (
    McpClassifiedDecision,
    ProbedMcpServer,
    ProbedMcpTool,
)


class _FakeProbe:
    def __init__(self, servers: dict[str, ProbedMcpServer]) -> None:
        self.servers: dict[str, ProbedMcpServer] = servers
        self.calls: list[str] = []

    async def probe(self, spec: McpServerSpec) -> ProbedMcpServer:
        self.calls.append(spec.name)
        return self.servers[spec.name]


class _FakeClassifier:
    def __init__(self) -> None:
        self.batches: list[tuple[str, tuple[str, ...]]] = []

    async def classify(
        self,
        server: ProbedMcpServer,
        tools: Sequence[ProbedMcpTool],
    ) -> Sequence[McpClassifiedDecision]:
        self.batches.append((server.server_name, tuple(t.name for t in tools)))
        out: list[McpClassifiedDecision] = []
        for t in tools:
            if t.annotations.get("readOnlyHint") is True:
                out.append(
                    McpClassifiedDecision(
                        tool_name=t.name,
                        level="pass",
                        lock_mode="shared",
                        recommended_roles=("researcher", "coder"),
                        reason="read-only",
                    )
                )
            else:
                out.append(
                    McpClassifiedDecision(
                        tool_name=t.name,
                        level="review",
                        lock_mode="exclusive",
                        recommended_roles=("coder",),
                        reason="mutating",
                    )
                )
        return out


def _make_tool(server_name: str, name: str, desc: str, *, read_only: bool) -> ProbedMcpTool:
    schema = cast(dict[str, object], {"type": "object", "properties": {"q": {"type": "string"}}})
    ann = cast(dict[str, object], {"readOnlyHint": read_only})
    return ProbedMcpTool(
        name=name,
        description=desc,
        input_schema=schema,
        annotations=ann,
        schema_hash=compute_tool_schema_hash(
            server_name=server_name,
            tool_name=name,
            description=desc,
            input_schema=schema,
            annotations=ann,
        ),
    )


class TestMcpConfig(unittest.TestCase):
    def test_merge_override_dedup_and_desensitized_fingerprint(self) -> None:
        global_cfg = {
            "mcpServers": {
                "docs": {
                    "url": "https://docs.example.com/mcp/",
                    "auth": {"type": "bearer", "token": "${TOKEN_A}"},
                    "roles": ["researcher"],
                },
                "shared_alias": {
                    "command": "uvx",
                    "args": ["mcp-server-git"],
                },
            }
        }
        workspace_cfg = {
            "mcpServers": {
                "docs": {
                    "url": "https://docs.example.com/mcp",
                    "auth": {"type": "bearer", "token": "${TOKEN_B}"},
                    "roles": ["researcher", "coder"],
                },
                "git_local": {
                    "command": "uvx",
                    "args": ["mcp-server-git"],
                },
            }
        }

        merged_a = merge_mcp_configs(
            global_config=global_cfg,
            workspace_config=workspace_cfg,
            environ={"TOKEN_A": "secret-1", "TOKEN_B": "secret-2"},
        )
        merged_b = merge_mcp_configs(
            global_config=global_cfg,
            workspace_config=workspace_cfg,
            environ={"TOKEN_A": "secret-1", "TOKEN_B": "rotated-secret-999"},
        )

        self.assertEqual(merged_a.overridden_names, ("docs",))
        self.assertEqual(merged_a.deduplicated_names, ("shared_alias",))
        by_name = {s.name: s for s in merged_a.servers}
        self.assertEqual(set(by_name), {"docs", "git_local"})
        self.assertEqual(by_name["docs"].scope, "workspace")
        self.assertEqual(by_name["docs"].roles, ("researcher", "coder"))
        by_name_b = {s.name: s for s in merged_b.servers}
        self.assertEqual(by_name["docs"].endpoint_fingerprint, by_name_b["docs"].endpoint_fingerprint)

    def test_load_does_not_inherit_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            parent_cfg = tmp_path / ".agent" / "mcp.json"
            parent_cfg.parent.mkdir(parents=True)
            _ = parent_cfg.write_text(
                json.dumps({"mcpServers": {"parent_only": {"url": "https://parent.example/mcp"}}}),
                encoding="utf-8",
            )
            workspace = tmp_path / "subproject"
            workspace.mkdir()
            global_cfg = tmp_path / "global_mcp.json"
            _ = global_cfg.write_text(
                json.dumps({"mcpServers": {"global_srv": {"url": "https://global.example/mcp"}}}),
                encoding="utf-8",
            )

            loaded = load_mcp_configs(global_path=global_cfg, workspace_root=workspace)
            self.assertEqual([s.name for s in loaded.servers], ["global_srv"])


class TestMcpAuthAndCompiler(unittest.IsolatedAsyncioTestCase):
    async def test_auth_builder_and_token_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_root = Path(tmp) / "tokens"
            fernet_key = Fernet.generate_key()
            merged = merge_mcp_configs(
                global_config={
                    "mcpServers": {
                        "oauth_srv": {
                            "url": "https://oauth.example.com/mcp",
                            "auth": {
                                "type": "oauth",
                                "scopes": ["read", "write"],
                                "client_metadata_url": "https://app.example.com/client.json",
                            },
                        },
                        "m2m_srv": {
                            "url": "https://m2m.example.com/mcp",
                            "auth": {
                                "type": "oauth_client_credentials",
                                "client_id": "cid",
                                "client_secret": "csec",
                                "scopes": ["api"],
                            },
                        },
                        "bearer_srv": {
                            "url": "https://bearer.example.com/mcp",
                            "auth": {"type": "bearer", "token": "tok-123"},
                        },
                    }
                },
                workspace_config={
                    "mcpServers": {
                        "oauth_srv": {
                            "url": "https://oauth.example.com/mcp",
                            "auth": {"type": "oauth", "scopes": ["read"]},
                        }
                    }
                },
            )
            specs = {s.name: s for s in merged.servers}
            self.assertIsInstance(
                build_fastmcp_auth(specs["bearer_srv"], token_root=token_root),
                BearerAuth,
            )
            self.assertIsInstance(
                build_fastmcp_auth(specs["m2m_srv"], token_root=token_root),
                ClientCredentialsOAuthProvider,
            )
            oauth_auth: object = build_fastmcp_auth(
                specs["oauth_srv"], token_root=token_root, encryption_key=fernet_key
            )
            self.assertIsInstance(oauth_auth, OAuth)

            checker = LocalOAuthReadinessChecker(token_root=token_root)
            self.assertFalse(await checker.is_ready(specs["oauth_srv"]))
            self.assertTrue(await checker.is_ready(specs["m2m_srv"]))
            self.assertTrue(await checker.is_ready(specs["bearer_srv"]))

            store = build_token_storage(
                specs["oauth_srv"], token_root=token_root, encryption_key=fernet_key
            )
            await store.put(key="tok", value={"access_token": "abc"})
            self.assertTrue(await checker.is_ready(specs["oauth_srv"]))

            global_oauth_spec = merge_mcp_configs(
                global_config={
                    "mcpServers": {
                        "oauth_srv": {
                            "url": "https://oauth.example.com/mcp",
                            "auth": {"type": "oauth"},
                        }
                    }
                }
            ).servers[0]
            self.assertNotEqual(
                token_namespace_dir(global_oauth_spec, token_root=token_root),
                token_namespace_dir(specs["oauth_srv"], token_root=token_root),
            )

            cap = build_mcp_capability(specs["bearer_srv"], token_root=token_root)
            self.assertEqual(cap.id, "bearer_srv")

    async def test_incremental_compiler_cache_and_delta_reclassification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = create_sqlite_async_engine(tmp_path / "mcp.db")
            await init_async_db(engine)
            store = SQLModelMcpPolicyStore(engine)
            token_root = tmp_path / "tokens"

            merged = merge_mcp_configs(
                global_config={
                    "mcpServers": {
                        "docs": {"url": "https://docs.example.com/mcp"},
                        "git": {"command": "uvx", "args": ["mcp-git"]},
                        "needs_oauth": {
                            "url": "https://oauth.example.com/mcp",
                            "auth": {"type": "oauth"},
                        },
                    }
                }
            )
            probed_map = {
                "docs": ProbedMcpServer(
                    server_name="docs",
                    endpoint_fingerprint=next(
                        s.endpoint_fingerprint for s in merged.servers if s.name == "docs"
                    ),
                    server_impl_name="docs-mcp",
                    server_version="1.0.0",
                    tools=(_make_tool("docs", "search", "search docs", read_only=True),),
                ),
                "git": ProbedMcpServer(
                    server_name="git",
                    endpoint_fingerprint=next(
                        s.endpoint_fingerprint for s in merged.servers if s.name == "git"
                    ),
                    server_impl_name="git-mcp",
                    server_version="1.0.0",
                    tools=(
                        _make_tool("git", "status", "git status", read_only=True),
                        _make_tool("git", "commit", "git commit", read_only=False),
                    ),
                ),
            }
            probe = _FakeProbe(probed_map)
            classifier = _FakeClassifier()
            compiler = McpPolicyCompiler(
                probe=probe,
                classifier=classifier,
                store=store,
                auth_readiness=LocalOAuthReadinessChecker(token_root=token_root),
            )

            report1 = await compiler.compile_all(merged.servers)
            self.assertEqual(report1.total_jev_calls, 2)
            statuses1 = {e.server_name: e.status for e in report1.entries}
            self.assertEqual(
                statuses1,
                {"docs": "compiled", "git": "compiled", "needs_oauth": "auth_required"},
            )
            self.assertNotIn("needs_oauth", probe.calls)
            self.assertEqual(len(await store.list_all()), 3)

            classifier.batches.clear()
            report2 = await compiler.compile_all(merged.servers)
            self.assertEqual(report2.total_jev_calls, 0)
            self.assertEqual(classifier.batches, [])
            self.assertEqual(
                {e.server_name: e.status for e in report2.entries},
                {"docs": "cached", "git": "cached", "needs_oauth": "auth_required"},
            )

            probed_map["git"] = ProbedMcpServer(
                server_name="git",
                endpoint_fingerprint=probed_map["git"].endpoint_fingerprint,
                server_impl_name="git-mcp",
                server_version="1.0.0",
                tools=(
                    probed_map["git"].tools[0],
                    _make_tool("git", "commit", "git commit v2 with amend", read_only=False),
                ),
            )
            report3 = await compiler.compile_all(merged.servers)
            self.assertEqual(report3.total_jev_calls, 1)
            self.assertEqual(classifier.batches, [("git", ("commit",))])

            await engine.dispose()
