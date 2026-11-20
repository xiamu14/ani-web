"""任务完成质量与真实成功判定器（TaskOutcomeJudge，Layer 0 纯函数层，拦截“伪完成”）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

OutcomeReasonCode = Literal[
    "verified_success",
    "explicit_status_tag_failed",
    "unresolved_permission_block",
    "unresolved_command_failure",
    "soft_budget_incomplete",
    "empty_output",
]

_STATUS_TAG_RE = re.compile(
    r"\[TASK_STATUS:\s*(COMPLETED|FAILED|BLOCKED|INCOMPLETE)\]",
    re.IGNORECASE,
)
_NONZERO_EXIT_RE = re.compile(
    r"(?:exit(?:ed)?\s+with\s+(?:code|status)\s+[1-9]\d*|returncode[=:\s]+[1-9]\d*|FAILED\s*\((?:failures|errors)=\d+\))",
    re.IGNORECASE,
)
_VERBAL_FAILURE_RE = re.compile(
    r"(?:无法完成(?:该|此|当前)?任务|任务(?:执行)?失败|由于权限(?:被拒|不足)无法|cannot\s+complete\s+the\s+task|unable\s+to\s+complete\s+(?:the\s+)?task|task\s+cannot\s+be\s+completed)",
    re.IGNORECASE,
)
_INCOMPLETE_HINT_RE = re.compile(
    r"(?:尚未完成|未全部完成|由于预算限制未能完成|remaining\s+work\s+needed|not\s+yet\s+complete|partially\s+completed)",
    re.IGNORECASE,
)


@dataclass(frozen=True, kw_only=True)
class ToolCallRecord:
    """单次工具调用的执行结果摘要。"""

    tool_name: str
    canonical_args: str
    succeeded: bool
    is_command_tool: bool = False
    command_failed: bool = False
    error_preview: str = ""


@dataclass
class ToolExecutionTrace:
    """单次 Agent Loop 运行期间采集的工具轨迹与预算预警状态。"""

    records: list[ToolCallRecord] = field(default_factory=list)
    permission_denials: int = 0
    soft_limit_triggered: bool = False
    doom_loop_aborted: bool = False

    @property
    def successful_tool_calls(self) -> int:
        return sum(1 for r in self.records if r.succeeded and not r.command_failed)

    @property
    def has_unresolved_command_failure(self) -> bool:
        """检查最后一次命令/测试工具是否以失败告终，且其后没有任何成功的修复或验证操作。"""
        last_cmd_idx = -1
        for idx, rec in enumerate(self.records):
            if rec.is_command_tool:
                last_cmd_idx = idx
        if last_cmd_idx < 0:
            return False
        last_cmd = self.records[last_cmd_idx]
        if not last_cmd.command_failed:
            return False
        return not any(
            r.succeeded and not r.command_failed for r in self.records[last_cmd_idx + 1 :]
        )


@dataclass(frozen=True, kw_only=True)
class TaskOutcomeVerdict:
    """任务退出时的真实成败判定结论。"""

    succeeded: bool
    status: Literal["completed", "failed"]
    reason_code: OutcomeReasonCode
    summary: str
    allow_verified_memory: bool


def is_command_output_failed(result_text: str) -> bool:
    """从 shell / run_command 的返回文本检测非零退出码或测试框架失败标识。"""
    return bool(_NONZERO_EXIT_RE.search(result_text))


def evaluate_task_outcome(
    output: str,
    *,
    trace: ToolExecutionTrace | None = None,
    role_name: str = "role",
) -> TaskOutcomeVerdict:
    """综合最终输出文本、工具执行轨迹与预算软阈值状态判定任务是否真正成功。"""
    _ = role_name
    cleaned = output.strip()
    if not cleaned:
        return TaskOutcomeVerdict(
            succeeded=False,
            status="failed",
            reason_code="empty_output",
            summary="Task produced empty output.",
            allow_verified_memory=False,
        )

    active_trace = trace if trace is not None else ToolExecutionTrace()
    tag_matches: list[str] = [str(m.group(1)).upper() for m in _STATUS_TAG_RE.finditer(cleaned)]
    explicit_tag = tag_matches[-1] if tag_matches else None

    # 1. 显式失败/阻塞/未完成协议标签优先拦截
    if explicit_tag in ("FAILED", "BLOCKED", "INCOMPLETE"):
        reason: OutcomeReasonCode = (
            "soft_budget_incomplete"
            if (explicit_tag == "INCOMPLETE" and active_trace.soft_limit_triggered)
            else "explicit_status_tag_failed"
        )
        return TaskOutcomeVerdict(
            succeeded=False,
            status="failed",
            reason_code=reason,
            summary=cleaned,
            allow_verified_memory=False,
        )

    # 2. 权限拒绝未解拦截：发生过权限拒绝，且无任何成功工具执行（或口头报告失败）
    if active_trace.permission_denials > 0 and (
        active_trace.successful_tool_calls == 0 or bool(_VERBAL_FAILURE_RE.search(cleaned))
    ):
        return TaskOutcomeVerdict(
            succeeded=False,
            status="failed",
            reason_code="unresolved_permission_block",
            summary=cleaned,
            allow_verified_memory=False,
        )

    # 3. 末端命令/测试失败未解拦截（除非显式声明 [TASK_STATUS: COMPLETED] 证明失败是预期的）
    if (
        active_trace.has_unresolved_command_failure
        and explicit_tag != "COMPLETED"
        and (bool(_VERBAL_FAILURE_RE.search(cleaned)) or bool(_NONZERO_EXIT_RE.search(cleaned)))
    ):
        return TaskOutcomeVerdict(
            succeeded=False,
            status="failed",
            reason_code="unresolved_command_failure",
            summary=cleaned,
            allow_verified_memory=False,
        )

    # 4. 触发 80% 软预算警告后，输出文本自述尚未完成
    if (
        active_trace.soft_limit_triggered
        and explicit_tag != "COMPLETED"
        and bool(_INCOMPLETE_HINT_RE.search(cleaned))
    ):
        return TaskOutcomeVerdict(
            succeeded=False,
            status="failed",
            reason_code="soft_budget_incomplete",
            summary=cleaned,
            allow_verified_memory=False,
        )

    # 5. 零成功工具调用且输出文本为明确口头报错
    if (
        active_trace.successful_tool_calls == 0
        and explicit_tag != "COMPLETED"
        and bool(_VERBAL_FAILURE_RE.search(cleaned))
    ):
        return TaskOutcomeVerdict(
            succeeded=False,
            status="failed",
            reason_code="explicit_status_tag_failed",
            summary=cleaned,
            allow_verified_memory=False,
        )

    return TaskOutcomeVerdict(
        succeeded=True,
        status="completed",
        reason_code="verified_success",
        summary=cleaned,
        allow_verified_memory=True,
    )
