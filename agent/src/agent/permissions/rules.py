"""权限规则声明层: 单一固定 Mode，仅维护 FORBIDDEN 与 REVIEW 规则（零 IO / 零 Runtime 依赖）。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

RuleLevel = Literal["pass", "review", "forbid"]


class McpRuleInfo(Protocol):
    """MCP 预编译规则只读视图（解耦具体存储与编译实现）。"""

    @property
    def server_name(self) -> str: ...

    @property
    def tool_name(self) -> str: ...

    @property
    def schema_hash(self) -> str: ...

    @property
    def level(self) -> RuleLevel: ...

    @property
    def reason(self) -> str: ...

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
_SAFE_SYSTEM_DEV_PATHS: frozenset[str] = frozenset(
    {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"}
)
_SHELL_OPERATORS: frozenset[str] = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "<<", "&"})
_REDIRECT_PREFIX_RE = re.compile(r"^(?:\d*>>?|\d*<|&>>?)")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


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
        return False
    return target.is_relative_to(workspace_root)


def _has_parent_segment(token: str) -> bool:
    return token == ".." or token.startswith("../") or token.endswith("/..") or "/../" in token


def _extract_candidate_tokens(raw_token: str) -> list[str]:
    stripped = _REDIRECT_PREFIX_RE.sub("", raw_token.strip()).strip("'\"")
    if not stripped or stripped in _SHELL_OPERATORS:
        return []
    if "=" in stripped and not stripped.startswith(("/", "~")):
        _, _, value = stripped.partition("=")
        return [value.strip("'\"")] if value else []
    return [stripped]


def _find_outside_workspace_cmd_path(cmd_text: str, workspace_root: Path) -> str | None:
    try:
        raw_tokens = shlex.split(cmd_text, posix=True)
    except ValueError:
        raw_tokens = cmd_text.split()

    for raw_tok in raw_tokens:
        for tok in _extract_candidate_tokens(raw_tok):
            if not tok or _URL_SCHEME_RE.match(tok):
                continue
            if tok == "~" or tok.startswith("~/"):
                resolved = Path(tok).expanduser().resolve()
                if not resolved.is_relative_to(workspace_root):
                    return tok
            elif _has_parent_segment(tok):
                resolved = (workspace_root / tok).resolve()
                if not resolved.is_relative_to(workspace_root):
                    return tok
            elif tok.startswith("/") and not tok.startswith("//"):
                if tok in _SAFE_SYSTEM_DEV_PATHS:
                    continue
                resolved = Path(tok).resolve()
                if not resolved.is_relative_to(workspace_root):
                    return tok
    return None


def evaluate_tool_rule(
    tool_name: str,
    args: Mapping[str, object],
    workspace_root: str | Path | None = None,
    *,
    mcp_policy: McpRuleInfo | None = None,
    is_mcp_tool: bool = False,
    runtime_schema_hash: str | None = None,
) -> RuleVerdict:
    """纯函数规则判定: 按单一固定 Mode 返回 pass / review / forbid。"""
    # 0. MCP 工具 O(1) 预编译查表（未登记或 Schema 漂移一律兜底 REVIEW）
    if is_mcp_tool or mcp_policy is not None:
        if mcp_policy is None:
            return RuleVerdict(
                level="review",
                rule_id="review.mcp.unregistered",
                category="mcp_unregistered",
                reason=f"未登记的 MCP 工具需审核确认: {tool_name}",
            )
        if runtime_schema_hash is not None and runtime_schema_hash != mcp_policy.schema_hash:
            return RuleVerdict(
                level="review",
                rule_id="review.mcp.schema_drift",
                category="mcp_schema_drift",
                reason=f"MCP 工具 Schema 发生漂移，需审核确认: {tool_name}",
            )
        return RuleVerdict(
            level=mcp_policy.level,
            rule_id=f"{mcp_policy.level}.mcp.{mcp_policy.server_name}.{mcp_policy.tool_name}",
            category=f"mcp:{mcp_policy.server_name}",
            reason=mcp_policy.reason or f"MCP 预编译策略: {mcp_policy.level}",
        )

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
        if norm_ws is None:
            return RuleVerdict(
                level="review",
                rule_id="review.cmd.no_workspace_shell",
                category="out_of_workspace_shell",
                reason="未绑定受限工作区，执行 Shell 命令需审核确认",
            )
        outside_tok = _find_outside_workspace_cmd_path(cmd_text, norm_ws)
        if outside_tok is not None:
            return RuleVerdict(
                level="review",
                rule_id="review.cmd.outside_workspace_access",
                category="out_of_workspace_shell",
                reason=f"Shell 命令引用或执行受限工作区外路径，需审核确认: {outside_tok}",
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

        if norm_ws is None:
            if tool_name in _WRITE_TOOLS:
                return RuleVerdict(
                    level="forbid",
                    rule_id="forbid.path.no_workspace_write",
                    category="out_of_workspace_write",
                    reason=f"未绑定受限工作区，禁止执行写/改操作: {target}",
                )
            if tool_name in _READ_TOOLS:
                return RuleVerdict(
                    level="review",
                    rule_id="review.path.no_workspace_read",
                    category="out_of_workspace_read",
                    reason=f"未绑定受限工作区，读取本地路径需审核确认: {target}",
                )
        elif not _is_within_workspace(target, norm_ws):
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
    elif norm_ws is None:
        if tool_name in _WRITE_TOOLS:
            return RuleVerdict(
                level="forbid",
                rule_id="forbid.path.no_workspace_write",
                category="out_of_workspace_write",
                reason="未绑定受限工作区，禁止执行写/改操作",
            )
        if tool_name in _READ_TOOLS:
            return RuleVerdict(
                level="review",
                rule_id="review.path.no_workspace_read",
                category="out_of_workspace_read",
                reason="未绑定受限工作区，读取本地路径需审核确认",
            )

    return RuleVerdict(level="pass")
