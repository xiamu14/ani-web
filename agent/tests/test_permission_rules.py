"""Step 5.1 单元测试: 权限 Rules 声明与纯函数判定（FORBIDDEN / REVIEW / PASS）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.permissions import evaluate_tool_rule


class TestPermissionRules(unittest.TestCase):
    """验证单一固定 Mode 下的禁止规则、审核范围规则与工作区内正常操作放行。"""

    def test_forbidden_commands_and_sensitive_paths(self) -> None:
        ws = Path("/tmp/demo_ws")

        v1 = evaluate_tool_rule("shell", {"command": "rm -rf /"}, workspace_root=ws)
        self.assertEqual(v1.level, "forbid")

        v2 = evaluate_tool_rule("run_command", {"command": "git push origin main --force"}, workspace_root=ws)
        self.assertEqual(v2.level, "forbid")

        v3 = evaluate_tool_rule("read_file", {"path": ".env"}, workspace_root=ws)
        self.assertEqual(v3.level, "forbid")

        v4 = evaluate_tool_rule("read_file", {"path": "~/.ssh/id_rsa"}, workspace_root=ws)
        self.assertEqual(v4.level, "forbid")

        v5 = evaluate_tool_rule("shell", {"command": "cat .env.production"}, workspace_root=ws)
        self.assertEqual(v5.level, "forbid")

        v6 = evaluate_tool_rule("set_workspace", {"path": "~/.ssh"}, workspace_root=ws)
        self.assertEqual(v6.level, "forbid")

        v7 = evaluate_tool_rule("shell", {"command": "cat <<EOF > pyproject.toml\nname='x'\nEOF"}, workspace_root=ws)
        self.assertEqual(v7.level, "review")

    def test_out_of_workspace_write_forbidden_and_read_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            outside = Path(tmp) / "outside"
            ws.mkdir()
            outside.mkdir()

            vw = evaluate_tool_rule("write_file", {"path": str(outside / "x.py")}, workspace_root=ws)
            self.assertEqual(vw.level, "forbid")
            self.assertEqual(vw.rule_id, "forbid.path.outside_workspace_write")

            vr = evaluate_tool_rule("read_file", {"path": str(outside / "notes.md")}, workspace_root=ws)
            self.assertEqual(vr.level, "review")
            self.assertEqual(vr.rule_id, "review.path.outside_workspace_read")

    def test_review_commands_and_critical_configs(self) -> None:
        ws = Path("/tmp/demo_ws")

        v1 = evaluate_tool_rule("shell", {"command": "uv add httpx"}, workspace_root=ws)
        self.assertEqual(v1.level, "review")

        v2 = evaluate_tool_rule("run_command", {"command": "curl https://example.com"}, workspace_root=ws)
        self.assertEqual(v2.level, "review")

        v3 = evaluate_tool_rule("shell", {"command": "rm old_file.py"}, workspace_root=ws)
        self.assertEqual(v3.level, "review")

        v4 = evaluate_tool_rule("edit_file", {"path": "pyproject.toml"}, workspace_root=ws)
        self.assertEqual(v4.level, "review")
        self.assertEqual(v4.rule_id, "review.path.project_config")

        v5 = evaluate_tool_rule("write_file", {"path": "db/init.sql"}, workspace_root=ws)
        self.assertEqual(v5.level, "review")

    def test_normal_in_workspace_operations_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()

            self.assertEqual(
                evaluate_tool_rule("read_file", {"path": "src/app.py"}, workspace_root=ws).level,
                "pass",
            )
            self.assertEqual(
                evaluate_tool_rule("write_file", {"path": "src/app.py"}, workspace_root=ws).level,
                "pass",
            )
            self.assertEqual(
                evaluate_tool_rule("shell", {"command": "python3 -m unittest"}, workspace_root=ws).level,
                "pass",
            )
            self.assertEqual(
                evaluate_tool_rule("shell", {"command": "git diff HEAD~1"}, workspace_root=ws).level,
                "pass",
            )
            self.assertEqual(
                evaluate_tool_rule("shell", {"command": "python3 scripts/a.py > /dev/null"}, workspace_root=ws).level,
                "pass",
            )

    def test_shell_outside_workspace_paths_require_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            outside = Path(tmp) / "other"
            ws.mkdir()
            outside.mkdir()

            v_home = evaluate_tool_rule(
                "shell",
                {"command": "python3 ~/.agent/skills/demo/scripts/run.py"},
                workspace_root=ws,
            )
            self.assertEqual(v_home.level, "review")
            self.assertEqual(v_home.rule_id, "review.cmd.outside_workspace_access")

            v_parent = evaluate_tool_rule(
                "shell",
                {"command": "cat ../other/a.txt"},
                workspace_root=ws,
            )
            self.assertEqual(v_parent.level, "review")
            self.assertEqual(v_parent.rule_id, "review.cmd.outside_workspace_access")

            v_abs = evaluate_tool_rule(
                "shell",
                {"command": f"python3 {outside / 'a.py'}"},
                workspace_root=ws,
            )
            self.assertEqual(v_abs.level, "review")
            self.assertEqual(v_abs.rule_id, "review.cmd.outside_workspace_access")

    def test_no_workspace_bound_fails_closed(self) -> None:
        vw = evaluate_tool_rule("write_file", {"path": "a.py"}, workspace_root=None)
        self.assertEqual(vw.level, "forbid")
        self.assertEqual(vw.rule_id, "forbid.path.no_workspace_write")

        vr = evaluate_tool_rule("read_file", {"path": "a.py"}, workspace_root=None)
        self.assertEqual(vr.level, "review")
        self.assertEqual(vr.rule_id, "review.path.no_workspace_read")

        vs = evaluate_tool_rule("shell", {"command": "python3 -m unittest"}, workspace_root=None)
        self.assertEqual(vs.level, "review")
        self.assertEqual(vs.rule_id, "review.cmd.no_workspace_shell")


if __name__ == "__main__":
    _ = unittest.main()
