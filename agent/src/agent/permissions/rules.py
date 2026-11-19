"""权限规则声明层: 单一固定 Mode，仅维护 FORBIDDEN 与 REVIEW 规则（零 IO / 零 Runtime 依赖）。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RuleLevel = Literal["pass", "review", "forbid"]

_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "file_info",
        "list_directory",
        "list_files",
        "search_files",
        "find_files",
        "grep",
    }
)
_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "create_directory"})
_WORKSPACE_BIND_TOOLS: frozenset[str] = frozenset({"set_workspace", "dispatch_role_agent"})
_SHELL_TOOLS: frozenset[str] = frozenset({"shell", "run_command", "start_command"})
_PATH_ARG_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "target_file",
    "directory",
    "dir_path",
    "work_path",
)
_CMD_ARG_KEYS: tuple[str, ...] = ("command", "cmd", "script")


@dataclass(frozen=True, kw_only=True)
class RuleVerdict:
    """静态规则匹配结果。"""

    level: RuleLevel
    rule_id: str | None = None
    category: str = ""
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class CommandRule:
    rule_id: str
    category: str
    reason: str
    pattern: re.Pattern[str]


FORBIDDEN_COMMAND_RULES: tuple[CommandRule, ...] = (
    CommandRule(
        rule_id="forbid.cmd.destructive_root",
        category="destructive_command",
        reason="禁止执行根目录/主目录递归强删、格式化或关机等破坏性命令",
        pattern=re.compile(
            r"(?:^|[;&|\s])(?:rm\s+-[rf]{1,2}\s+(?:/|~|\*|\.\.)|mkfs\.|dd\s+if=|shutdown\b|reboot\b|:\(\)\{)",
            re.IGNORECASE,
        ),
    ),
    CommandRule(
        rule_id="forbid.cmd.git_destructive",
        category="destructive_git",
        reason="禁止执行不可逆的 Git 强推或硬重置命令",
        pattern=re.compile(
            r"\bgit\s+(?:push\s+.*(?:--force|-f)\b|reset\s+--hard\b)",
            re.IGNORECASE,
        ),
    ),
)

REVIEW_COMMAND_RULES: tuple[CommandRule, ...] = (
    CommandRule(
        rule_id="review.cmd.package_or_network",
        category="external_network_or_pkg",
        reason="涉及包安装或外部网络请求，需审核风险",
        pattern=re.compile(
            r"\b(?:pip|uv|npm|pnpm|yarn|brew)\s+(?:install|add|remove|uninstall)\b|\b(?:curl|wget)\b",
            re.IGNORECASE,
        ),
    ),
    CommandRule(
        rule_id="review.cmd.file_mutation_or_proc",
        category="shell_mutation",
        reason="涉及文件删除、权限变更、进程终止或数据库操作，需审核确认",
        pattern=re.compile(
            r"\b(?:rm|mv|chmod|chown|kill|pkill|sqlite3)\b|\bgit\s+(?:push|clean|checkout\s+--)\b",
            re.IGNORECASE,
        ),
    ),
    CommandRule(
        rule_id="review.cmd.redirect_or_inplace_config",
        category="critical_config_write",
        reason="涉及通过 Shell 重定向、tee 或 sed -i 修改关键配置或外部路径，需审核确认",
        pattern=re.compile(
            r"(?:>>?|\btee\b|\bsed\s+-i\b).*(?:pyproject\.toml|package\.json|Dockerfile|AGENTS\.md|\.sql|/etc/|/usr/)",
            re.IGNORECASE,
        ),
    ),
)

FORBIDDEN_SENSITIVE_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "forbid.path.env_secrets",
        re.compile(r"(?:^|[/\s'\"=])\.env(?:\.[A-Za-z0-9_.-]+)?(?=$|[\s'\"])", re.IGNORECASE),
    ),
    (
        "forbid.path.ssh_keys",
        re.compile(r"(?:^|[/\s'\"])\.ssh(?:/(?:id_[^/\s'\"]+|authorized_keys|known_hosts))?(?=$|[\s'\"])"),
    ),
    ("forbid.path.cloud_creds", re.compile(r"(?:^|[/\s'\"])\.(?:aws|kube|gnupg)(?:/|$|[\s'\"])")),
    (
        "forbid.path.system_auth",
        re.compile(r"(?:^|[\s'\"])/(?:etc(?:/(?:shadow|sudoers))?|System|usr|sbin)(?=$|/|[\s'\"])"),
    ),
)

REVIEW_SENSITIVE_CONFIG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "review.path.project_config",
        re.compile(r"(?:^|/)(?:pyproject\.toml|package\.json|Dockerfile|AGENTS\.md)$"),
    ),
    ("review.path.ci_or_sql", re.compile(r"(?:^|/)\.github/workflows/|\.sql$")),
)


def _extract_str_arg(args: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _resolve_target_path(raw_path: str, workspace_root: Path | None) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute() or workspace_root is None:
        return candidate.resolve()
    return (workspace_root / candidate).resolve()


def _is_within_workspace(target: Path, workspace_root: Path | None) -> bool:
    if workspace_root is None:
        return True
    return target.is_relative_to(workspace_root)


def evaluate_tool_rule(
    tool_name: str,
    args: Mapping[str, object],
    workspace_root: str | Path | None = None,
) -> RuleVerdict:
    """纯函数规则判定: 按单一固定 Mode 返回 pass / review / forbid。"""
    norm_ws = Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
    raw_path = _extract_str_arg(args, _PATH_ARG_KEYS)
    raw_cmd = _extract_str_arg(args, _CMD_ARG_KEYS)

    # 1. 命令类规则（FORBIDDEN 优先于 REVIEW）
    if tool_name in _SHELL_TOOLS or raw_cmd is not None:
        cmd_text = raw_cmd or ""
        for rule in FORBIDDEN_COMMAND_RULES:
            if rule.pattern.search(cmd_text):
                return RuleVerdict(
                    level="forbid",
                    rule_id=rule.rule_id,
                    category=rule.category,
                    reason=rule.reason,
                )
        for rule_id, pat in FORBIDDEN_SENSITIVE_PATH_PATTERNS:
            if pat.search(cmd_text):
                return RuleVerdict(
                    level="forbid",
                    rule_id=rule_id,
                    category="sensitive_data",
                    reason="禁止通过命令访问敏感凭证或密钥文件",
                )
        for rule in REVIEW_COMMAND_RULES:
            if rule.pattern.search(cmd_text):
                return RuleVerdict(
                    level="review",
                    rule_id=rule.rule_id,
                    category=rule.category,
                    reason=rule.reason,
                )

    # 2. 路径与读写/数据敏感度规则
    if raw_path is not None:
        target = _resolve_target_path(raw_path, norm_ws)
        target_str = str(target)

        for rule_id, pat in FORBIDDEN_SENSITIVE_PATH_PATTERNS:
            if pat.search(target_str) or pat.search(raw_path):
                return RuleVerdict(
                    level="forbid",
                    rule_id=rule_id,
                    category="sensitive_data",
                    reason=f"禁止访问或绑定敏感凭证/系统目录: {raw_path}",
                )

        if tool_name in _WORKSPACE_BIND_TOOLS:
            return RuleVerdict(level="pass")

        if not _is_within_workspace(target, norm_ws):
            if tool_name in _WRITE_TOOLS:
                return RuleVerdict(
                    level="forbid",
                    rule_id="forbid.path.outside_workspace_write",
                    category="out_of_workspace_write",
                    reason=f"禁止在受限工作区外执行写/改操作: {target}",
                )
            if tool_name in _READ_TOOLS:
                return RuleVerdict(
                    level="review",
                    rule_id="review.path.outside_workspace_read",
                    category="out_of_workspace_read",
                    reason=f"尝试读取受限工作区外路径，需审核确认: {target}",
                )

        if tool_name in _WRITE_TOOLS:
            for rule_id, pat in REVIEW_SENSITIVE_CONFIG_PATTERNS:
                if pat.search(target_str) or pat.search(raw_path):
                    return RuleVerdict(
                        level="review",
                        rule_id=rule_id,
                        category="critical_config_write",
                        reason=f"修改核心配置/规则/SQL 文件，需审核风险: {raw_path}",
                    )

    return RuleVerdict(level="pass")
