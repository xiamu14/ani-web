"""真实 LLM Role Agent E2E 验证：工作区 + 全局 Skills 渐进披露与 KV Cache 观测。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, override

import logfire
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    InstructionDeltaPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.workspaces import LocalWorkspaceBackend

from agent.config import load_settings
from agent.context import JevDecisionClient
from agent.core import build_default_role_registry, build_model, configure_observability
from agent.extensions import build_skills_capability
from agent.hooks import HookPriority, PermissionGuardHook
from agent.permissions import PermissionReviewer
from agent.tools import ToolExecutionQueueHook

_TARGET_MODULE_CODE = '''\
"""Order domain pricing, inventory sync, and creation helpers."""


def calculate_order_pricing(
    items: list[dict[str, object]],
    tier: str,
    coupon: str | None,
    rush_shipping: bool,
    tax_exempt: bool,
    region: str,
) -> float:
    """Compute final order total with tiered discounts and surcharges."""
    if not items:
        return 0.0
    subtotal = 0.0
    for item in items:
        qty = int(item.get("qty", 0) or 0)
        price = float(item.get("price", 0.0) or 0.0)
        if qty <= 0:
            continue
        category = str(item.get("category", "general"))
        if category == "vip":
            subtotal += qty * price * 0.85
        elif category == "bulk":
            subtotal += qty * price * 0.90
        else:
            subtotal += qty * price
    if tier == "gold":
        subtotal *= 0.88
    elif tier == "silver":
        subtotal *= 0.94
    if coupon and subtotal > 100.0:
        subtotal -= 15.0
    if rush_shipping:
        subtotal += 25.0
    if not (tax_exempt or region == "OR"):
        subtotal *= 1.08
    return round(max(subtotal, 0.0), 2)


def sync_inventory(warehouse_id, delta):
    # TODO: replace bare except with explicit storage exception handling
    try:
        return {"warehouse": warehouse_id, "delta": delta, "ok": True}
    except:
        return {"warehouse": warehouse_id, "delta": 0, "ok": False}


def create_order(customer_id: str, total: float) -> dict[str, object]:
    """Create a validated order payload."""
    return {"customer_id": customer_id, "total": total, "status": "created"}
'''

_AST_METRICS_SCRIPT = '''\
"""Compute real AST McCabe complexity, annotations, docstrings, and TODO metrics."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

_BRANCH_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While,
    ast.ExceptHandler, ast.With, ast.AsyncWith, ast.IfExp,
)


def _mccabe(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1
    for node in ast.walk(func):
        if isinstance(node, _BRANCH_NODES):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += max(len(node.values) - 1, 1)
        elif isinstance(node, ast.comprehension):
            score += 1 + len(node.ifs)
    return score


def analyze_file(path: Path) -> dict[str, object]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    functions: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            has_bare_except = any(
                isinstance(sub, ast.ExceptHandler) and sub.type is None for sub in ast.walk(node)
            )
            missing_ann = node.returns is None or any(
                arg.annotation is None for arg in node.args.args if arg.arg != "self"
            )
            functions.append({
                "name": node.name,
                "lineno": node.lineno,
                "mccabe_complexity": _mccabe(node),
                "has_docstring": ast.get_docstring(node) is not None,
                "missing_annotations": missing_ann,
                "has_bare_except": has_bare_except,
            })
    todo_count = sum(1 for line in source.splitlines() if "TODO" in line)
    return {"file": str(path), "todo_count": todo_count, "functions": functions}


if __name__ == "__main__":
    print(json.dumps(analyze_file(Path(sys.argv[1])), indent=2, ensure_ascii=False))
'''

_RUBRIC_DOC = """# Python AST Audit Rubric

Exact compliance tag (must be copied verbatim into `audit_report.md`):
`AUDIT_RUBRIC_TAG: PY-AST-V1`

## McCabe Complexity Thresholds
- `<= 5`: `GRADE-A` (low risk)
- `6 .. 9`: `GRADE-B` (moderate complexity, monitor)
- `>= 10`: `GRADE-C` (high complexity, refactor recommended)
"""

_WORKSPACE_AUDIT_SKILL = """---
name: py-module-auditor
description: Audit Python module AST complexity and code quality grades using the project rubric and AST metrics script.
when_to_use: Use when asked to audit Python code quality, AST complexity, or function grades.
version: 1.1.0
---

Follow these exact steps in order:
1. Call `read_file` to read `references/audit_rubric.md` inside this skill's directory (`Skill directory`).
2. Call `shell` to execute:
   `python3 "<Skill directory>/scripts/ast_metrics.py" <target_py_file>`
   (replace `<Skill directory>` with the directory path shown above and `<target_py_file>` with the module path).
3. Write or update `audit_report.md` in the workspace root with section `## AST Quality Audit`, including:
   - the exact tag line from `audit_rubric.md` (`AUDIT_RUBRIC_TAG: ...`),
   - every function's `mccabe_complexity` and corresponding `GRADE-A` / `GRADE-B` / `GRADE-C` rating.
"""

_GLOBAL_RELEASE_SKILL = """---
name: release-readiness-check
description: Evaluate Python module release readiness gate (bare except, annotations, TODOs) and recommend SemVer bump.
when_to_use: Use when asked to assess release readiness, release gate status, or SemVer version bump.
version: 1.0.0
---

Follow these exact steps in order:
1. Inspect the target Python module with `read_file` for bare `except:` blocks, missing type annotations, and `TODO` comments.
2. Apply the release gate matrix strictly:
   - If ANY bare `except:` block exists -> `RELEASE_GATE: BLOCKED` and `SEMVER_BUMP: none (fix blockers first)`
   - Else if missing type annotations or `TODO` exists -> `RELEASE_GATE: CONDITIONAL` and `SEMVER_BUMP: patch`
   - Else -> `RELEASE_GATE: READY` and `SEMVER_BUMP: minor`
   Include the exact policy anchor line: `GLOBAL_POLICY_ANCHOR: REL-CHECK-2026`.
3. Write or update section `## Release Readiness` in `audit_report.md` (preserving any existing sections), reporting `GLOBAL_POLICY_ANCHOR: REL-CHECK-2026`, `RELEASE_GATE`, `SEMVER_BUMP`, and specific line-level findings.
"""

_DISTRACTOR_SQL_SKILL = """---
name: sql-migration-planner
description: Plan zero-downtime PostgreSQL DDL schema migrations with expand-contract phases.
when_to_use: Use only when planning SQL DDL or database schema migrations.
version: 1.0.0
---

SQL_MIGRATION_SECRET_NEVER_LOAD
1. Create shadow columns before backfilling.
2. Enforce lock_timeout = '2s' before ALTER TABLE.
"""

_FORBIDDEN_IN_TURN0 = (
    "AUDIT_RUBRIC_TAG",
    "ast_metrics.py",
    "GLOBAL_POLICY_ANCHOR",
    "SQL_MIGRATION_SECRET_NEVER_LOAD",
)


@dataclass(frozen=True, kw_only=True)
class TurnTelemetry:
    """Single LLM request/response KV cache & tool-call record."""

    turn: int
    message_count: int
    instr_len: int
    instr_sha: str
    prefix_stable: bool
    history_append_only: bool
    visible_tools: tuple[str, ...]
    tool_calls: tuple[str, ...]
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    usage_details: dict[str, int]


@dataclass
class KvCacheTelemetryHook(AbstractCapability[object]):
    """观测每轮指令前缀哈希、历史 append-only 性质及服务端 KV Cache token 命中情况。"""

    hook_priority: int = int(HookPriority.OBSERVABILITY)
    turns: list[TurnTelemetry] = field(default_factory=list)
    turn0_instructions: str = ""
    loaded_skill_ids: list[str] = field(default_factory=list)
    called_tool_records: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    _baseline_sha: str | None = field(default=None, init=False, repr=False)
    _prev_msg_count: int = field(default=0, init=False, repr=False)
    _pending_turn: tuple[int, int, str, bool, bool, tuple[str, ...]] | None = field(
        default=None, init=False, repr=False
    )

    @override
    async def before_model_request(
        self, ctx: RunContext[object], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        _ = ctx
        msgs = request_context.messages
        last_req = next((m for m in reversed(msgs) if isinstance(m, ModelRequest)), None)
        instr = (last_req.instructions or "") if last_req is not None else ""
        sha = hashlib.sha256(instr.encode("utf-8")).hexdigest()[:12]
        if self._baseline_sha is None:
            self._baseline_sha = sha
            self.turn0_instructions = instr
        prefix_stable = sha == self._baseline_sha
        history_append_only = len(msgs) >= self._prev_msg_count
        self._prev_msg_count = len(msgs)
        tools = tuple(t.name for t in request_context.model_request_parameters.function_tools)
        self._pending_turn = (
            len(self.turns), len(msgs), sha, prefix_stable, history_append_only, tools,
        )
        return request_context

    @override
    async def after_model_request(
        self,
        ctx: RunContext[object],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        _ = ctx
        pending = self._pending_turn
        assert pending is not None
        turn_idx, msg_count, sha, prefix_stable, append_only, tools = pending
        last_req = next(
            (m for m in reversed(request_context.messages) if isinstance(m, ModelRequest)), None
        )
        instr_len = len((last_req.instructions or "") if last_req is not None else "")

        calls: list[str] = []
        for part in response.parts:
            if isinstance(part, ToolCallPart):
                args_dict = cast(dict[str, object], part.args_as_dict())
                self.called_tool_records.append((part.tool_name, args_dict))
                raw_id = args_dict.get("id")
                raw_path = args_dict.get("path")
                raw_cmd = args_dict.get("command")
                if part.tool_name == "load_capability" and isinstance(raw_id, str):
                    self.loaded_skill_ids.append(raw_id)
                    calls.append(f"load_capability({raw_id})")
                elif raw_path is not None:
                    calls.append(f"{part.tool_name}({Path(str(raw_path)).name})")
                elif raw_cmd is not None:
                    calls.append(f"{part.tool_name}({str(raw_cmd)[:42]})")
                else:
                    calls.append(part.tool_name)

        usage = response.usage
        self.turns.append(
            TurnTelemetry(
                turn=turn_idx,
                message_count=msg_count,
                instr_len=instr_len,
                instr_sha=sha,
                prefix_stable=prefix_stable,
                history_append_only=append_only,
                visible_tools=tools,
                tool_calls=tuple(calls),
                input_tokens=usage.input_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_write_1h_tokens=usage.cache_write_1h_tokens,
                output_tokens=usage.output_tokens,
                usage_details=dict(usage.details),
            )
        )
        return response


def _setup_fixture_workspace(root: Path) -> tuple[Path, Path]:
    ws = root / "workspace"
    fake_home = root / "fake_home"
    (ws / "src").mkdir(parents=True)
    _ = (ws / "src" / "order_service.py").write_text(_TARGET_MODULE_CODE, encoding="utf-8")

    ws_skill = ws / ".agent" / "skills" / "py-module-auditor"
    (ws_skill / "references").mkdir(parents=True)
    (ws_skill / "scripts").mkdir(parents=True)
    _ = (ws_skill / "SKILL.md").write_text(_WORKSPACE_AUDIT_SKILL, encoding="utf-8")
    _ = (ws_skill / "references" / "audit_rubric.md").write_text(_RUBRIC_DOC, encoding="utf-8")
    _ = (ws_skill / "scripts" / "ast_metrics.py").write_text(_AST_METRICS_SCRIPT, encoding="utf-8")

    sql_skill = ws / ".agent" / "skills" / "sql-migration-planner"
    sql_skill.mkdir(parents=True)
    _ = (sql_skill / "SKILL.md").write_text(_DISTRACTOR_SQL_SKILL, encoding="utf-8")

    global_skills = fake_home / ".agent" / "skills"
    rel_skill = global_skills / "release-readiness-check"
    rel_skill.mkdir(parents=True)
    _ = (rel_skill / "SKILL.md").write_text(_GLOBAL_RELEASE_SKILL, encoding="utf-8")
    return ws, global_skills


def _collect_loaded_instruction_deltas(messages: Sequence[ModelMessage]) -> list[str]:
    return [
        part.content
        for msg in messages
        for part in msg.parts
        if isinstance(part, InstructionDeltaPart) and part.content
    ]


def _print_report(
    *,
    model_name: str,
    catalog_summary: list[str],
    telemetry: KvCacheTelemetryHook,
    report_text: str,
    output_text: str,
) -> None:
    print("\n=== Role Agent Skills 渐进披露 & KV Cache E2E 观测报告 ===")
    print(f"Model: {model_name}")
    print("Resolved Skills:")
    for line in catalog_summary:
        print(f"  - {line}")

    header = (
        f"{'Turn':>4} | {'Msgs':>4} | {'InstrLen':>8} | {'InstrSHA':>12} | {'Stable':>6} | "
        + f"{'InTok':>7} | {'CacheRead':>9} | {'CacheW':>6} | {'HitRate':>7} | {'OutTok':>6} | ToolCalls"
    )
    print("\n" + header)
    print("-" * len(header))
    for t in telemetry.turns:
        hit_pct = (t.cache_read_tokens / t.input_tokens * 100.0) if t.input_tokens > 0 else 0.0
        calls_str = ", ".join(t.tool_calls) or "(final_text)"
        print(
            f"{t.turn:>4} | {t.message_count:>4} | {t.instr_len:>8} | {t.instr_sha:>12} | "
            + f"{t.prefix_stable!s:>6} | {t.input_tokens:>7} | {t.cache_read_tokens:>9} | "
            + f"{t.cache_write_tokens:>6} | {hit_pct:>6.1f}% | {t.output_tokens:>6} | {calls_str}"
        )
        if t.usage_details:
            print(f"       usage.details={t.usage_details}")

    subsequent = telemetry.turns[1:]
    sub_in = sum(t.input_tokens for t in subsequent)
    sub_read = sum(t.cache_read_tokens for t in subsequent)
    sub_rate = (sub_read / sub_in * 100.0) if sub_in > 0 else 0.0
    print(
        f"\nKV Summary: turns={len(telemetry.turns)}, "
        + f"total_in={sum(t.input_tokens for t in telemetry.turns)}, "
        + f"turn1+_in={sub_in}, turn1+_cache_read={sub_read} ({sub_rate:.1f}%)"
    )
    print(f"\n=== audit_report.md ===\n{report_text}")
    print(f"\n=== Role Final Output ===\n{output_text}")


async def run_e2e() -> None:
    _ = os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    settings = load_settings()
    logfire_enabled = configure_observability(settings)
    model = build_model(settings)
    coder_role = build_default_role_registry(model).get("coder")
    jev_client = (
        JevDecisionClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.decision_model_name,
        )
        if settings.openrouter_api_key
        else None
    )

    with tempfile.TemporaryDirectory() as tmp:
        ws, global_skills = _setup_fixture_workspace(Path(tmp))
        prompt = (
            "请对工作区中的订单服务模块 `src/order_service.py` 完成两项工作：\n"
            "1. 对模块执行 AST 复杂度与代码质量审计；\n"
            "2. 评估该模块当前是否具备发布上线就绪度与版本号升级建议。\n"
            "请按适用技能规范执行分析，并将两项完整评估章节汇总写入工作区根目录的 `audit_report.md`（保留双方章节，不要覆盖掉另一项）。"
        )
        perm_hook = PermissionGuardHook(
            reviewer=PermissionReviewer(jev_client=jev_client),
            workspace_root=ws,
            user_intent=prompt,
        )
        queue_hook = ToolExecutionQueueHook(workspace_root=ws)
        telemetry = KvCacheTelemetryHook()
        skills_cap = build_skills_capability(global_dirs=[global_skills], builtin_dirs=())
        agent: Agent[object, str] = coder_role.build_agent(
            extra_capabilities=[perm_hook, queue_hook, telemetry, skills_cap],
        )

        try:
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = await agent.run(prompt, workspace=LocalWorkspaceBackend(ws))
        finally:
            perm_hook.release_runs()
            if logfire_enabled:
                _ = logfire.force_flush()

        assert skills_cap.last_catalog is not None
        catalog_summary = [
            f"{s.name} (scope={s.scope.name}, version={s.version_raw}, dir={s.directory})"
            for s in skills_cap.last_catalog.skills
        ]
        report_path = ws / "audit_report.md"
        assert report_path.is_file(), "audit_report.md was not created"
        report_text = report_path.read_text(encoding="utf-8")

        _print_report(
            model_name=settings.model_name,
            catalog_summary=catalog_summary,
            telemetry=telemetry,
            report_text=report_text,
            output_text=result.output,
        )

        for skill_name in ("py-module-auditor", "release-readiness-check", "sql-migration-planner"):
            assert skill_name in telemetry.turn0_instructions, f"Missing L1 skill {skill_name}"
        for secret in _FORBIDDEN_IN_TURN0:
            assert secret not in telemetry.turn0_instructions, f"L2 leaked in turn 0: {secret}"

        loaded_set = set(telemetry.loaded_skill_ids)
        assert "py-module-auditor" in loaded_set, f"Workspace skill not loaded: {loaded_set}"
        assert "release-readiness-check" in loaded_set, f"Global skill not loaded: {loaded_set}"
        assert "sql-migration-planner" not in loaded_set, f"Distractor skill loaded: {loaded_set}"

        deltas = "\n".join(_collect_loaded_instruction_deltas(result.all_messages()))
        assert str((ws / ".agent" / "skills" / "py-module-auditor").resolve()) in deltas
        assert str((global_skills / "release-readiness-check").resolve()) in deltas

        read_paths = [
            str(args.get("path", ""))
            for name, args in telemetry.called_tool_records
            if name == "read_file"
        ]
        shell_cmds = [
            str(args.get("command", ""))
            for name, args in telemetry.called_tool_records
            if name == "shell"
        ]
        assert any("audit_rubric.md" in p for p in read_paths), f"L3 rubric not read: {read_paths}"
        assert any("ast_metrics.py" in c for c in shell_cmds), f"L3 script not run: {shell_cmds}"

        assert telemetry.turns, "No LLM turns recorded"
        assert all(t.prefix_stable for t in telemetry.turns), "Instructions prefix mutated mid-run!"
        assert all(t.history_append_only for t in telemetry.turns), "Message history shrank mid-run!"

        assert "AUDIT_RUBRIC_TAG: PY-AST-V1" in report_text
        assert "GRADE-C" in report_text
        assert "calculate_order_pricing" in report_text
        assert "GLOBAL_POLICY_ANCHOR: REL-CHECK-2026" in report_text
        assert "RELEASE_GATE: BLOCKED" in report_text
        print("\nE2E ASSERTIONS PASSED (all 6 checks OK).")


if __name__ == "__main__":
    asyncio.run(run_e2e())
