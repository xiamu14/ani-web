"""语义分支路由网关: 支持 OpenRouter TypeSafe Jev (/alpha/decisions) 与 Pydantic AI 模型。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, SystemPromptPart, TextPart, UserPromptPart
from pydantic_ai.models import KnownModelName, Model

from agent.context.tree_manager import BranchInfo
from agent.llm import JevHttpClient, build_decisions_url

__all__ = [
    "BranchAction",
    "BranchDecision",
    "JevDecisionClient",
    "SemanticBranchRouter",
    "build_decisions_url",
]

BranchAction = Literal["CONTINUE", "SWITCH", "FORK_HANDOFF", "FORK_ROOT"]

_VALID_ACTIONS: frozenset[str] = frozenset({"CONTINUE", "SWITCH", "FORK_HANDOFF", "FORK_ROOT"})

_CLASSIFIER_INSTRUCTIONS = """\
你是 Context Tree 的语义路由网关。根据当前活跃分支、已有分支列表、最近消息摘要与用户新输入，判定下一步分支动作：
- CONTINUE: 同一目标的延续（包括 Monorepo 内跨目录协作）。
- SWITCH: 用户明确要回到已有分支列表中的某个其他分支（填写 target_conversation_id）。
- FORK_HANDOFF: 基于当前分支结论派生关联子目标，需提取交接摘要 handoff_summary 与 new_title。
- FORK_ROOT: 与当前分支完全无关的新任务，需从 Root 新建分支并填写 new_title。
"""


class BranchDecision(BaseModel):
    """结构化语义路由判定结果。"""

    action: BranchAction = "CONTINUE"
    target_conversation_id: str | None = Field(default=None)
    new_title: str = Field(default="")
    handoff_summary: str = Field(default="")


def _recent_preview(messages: Sequence[ModelMessage], max_items: int = 4) -> str:
    lines: list[str] = []
    for msg in messages[-max_items:]:
        texts = [
            part.content[:80]
            for part in msg.parts
            if isinstance(part, (SystemPromptPart, UserPromptPart, TextPart)) and isinstance(part.content, str)
        ]
        if texts:
            lines.append(" | ".join(texts))
    return "\n".join(lines) or "(暂无消息)"


class JevDecisionClient(JevHttpClient):
    """OpenRouter TypeSafe Jev (/alpha/decisions) 语义分支决策客户端。"""

    async def decide(
        self,
        *,
        prompt: str,
        active_branch: BranchInfo,
        all_branches: Sequence[BranchInfo],
        recent_messages: Sequence[ModelMessage],
    ) -> BranchDecision:
        others = [b for b in all_branches if b.conversation_id != active_branch.conversation_id]
        recent_text = _recent_preview(recent_messages)
        state = {
            "active_branch": {
                "conversation_id": active_branch.conversation_id,
                "title": active_branch.target_title or "(未命名)",
                "work_path": active_branch.work_path or "(无)",
            },
            "other_branches": [
                {
                    "conversation_id": b.conversation_id,
                    "title": b.target_title or "(未命名)",
                    "work_path": b.work_path or "(无)",
                }
                for b in others
            ],
            "recent_messages": recent_text,
            "user_input": prompt,
        }
        questions: dict[str, object] = {
            "action": {
                "type": "choice",
                "instructions": "Decide how to route the user_input relative to active_branch and other_branches.",
                "criteria": {
                    "CONTINUE": "Continuation of the same task/goal as active_branch (including cross-directory monorepo work).",
                    "SWITCH": "User wants to switch back to one of the existing other_branches.",
                    "FORK_HANDOFF": "A related follow-up sub-target derived from active_branch conclusions.",
                    "FORK_ROOT": "A completely new task unrelated to active_branch or other_branches.",
                },
            }
        }
        if others:
            target_criteria = {
                b.conversation_id: f"Switch to branch {b.target_title or b.conversation_id} (work_path={b.work_path})"
                for b in others
            }
            target_criteria["NONE"] = "Not switching to any existing branch."
            questions["target_branch"] = {
                "type": "choice",
                "instructions": "If action is SWITCH, pick which conversation_id in other_branches matches.",
                "criteria": target_criteria,
            }

        data = await self.request_decisions(
            state=state,
            questions=questions,
            span_attrs={
                "active_conversation_id": active_branch.conversation_id,
                "other_branch_count": len(others),
                "user_input": prompt[:200],
            },
        )
        answers = cast(dict[str, dict[str, object]], data.get("answers", {}))
        action_answer = answers.get("action", {})
        raw_action = str(action_answer.get("choice", "CONTINUE"))
        action: BranchAction = cast(BranchAction, raw_action) if raw_action in _VALID_ACTIONS else "CONTINUE"

        raw_target = answers.get("target_branch", {}).get("choice")
        target_id = str(raw_target) if isinstance(raw_target, str) and raw_target != "NONE" else None
        resolved_target = target_id if action == "SWITCH" else None

        return BranchDecision(
            action=action,
            target_conversation_id=resolved_target,
            new_title=prompt.strip()[:40],
            handoff_summary=recent_text[:200] if action == "FORK_HANDOFF" else "",
        )


class SemanticBranchRouter:
    """轻量语义分支识别器（支持 JevDecisionClient 或通用 Pydantic AI Model）。"""

    def __init__(
        self,
        model: Model | KnownModelName | str | None = None,
        *,
        jev_client: JevDecisionClient | None = None,
    ) -> None:
        if jev_client is None and model is None:
            raise ValueError("SemanticBranchRouter 需要提供 model 或 jev_client。")
        self._jev_client: JevDecisionClient | None = jev_client
        self._agent: Agent[None, BranchDecision] | None = (
            Agent(
                model=model,
                output_type=BranchDecision,
                name="context_router",
                instructions=_CLASSIFIER_INSTRUCTIONS,
            )
            if jev_client is None and model is not None
            else None
        )

    async def decide(
        self,
        *,
        prompt: str,
        active_branch: BranchInfo,
        all_branches: Sequence[BranchInfo],
        recent_messages: Sequence[ModelMessage],
    ) -> BranchDecision:
        """对用户新输入执行语义分支判定。"""
        if self._jev_client is not None:
            return await self._jev_client.decide(
                prompt=prompt,
                active_branch=active_branch,
                all_branches=all_branches,
                recent_messages=recent_messages,
            )

        assert self._agent is not None
        others = [
            f"- {b.conversation_id}: title={b.target_title or '(未命名)'}, work_path={b.work_path or '(无)'}"
            for b in all_branches
            if b.conversation_id != active_branch.conversation_id
        ]
        classifier_prompt = (
            f"当前活跃分支: {active_branch.conversation_id} "
            f"(title={active_branch.target_title or '(未命名)'}, work_path={active_branch.work_path or '(无)'})\n"
            f"其他已有分支:\n{chr(10).join(others) if others else '(无)'}\n\n"
            f"最近消息摘要:\n{_recent_preview(recent_messages)}\n\n"
            f"用户新输入:\n{prompt}"
        )
        result = await self._agent.run(classifier_prompt)
        return result.output
