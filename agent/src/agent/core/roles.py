"""Role Agent 蓝图定义、Pydantic AI Harness 能力装配与任务级 Context Window 管理器。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict, ConversationStore
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai_harness import Coder, FileSystem, Shell
from pydantic_ai_harness.step_persistence import StepPersistence, StepStore

from agent.core.budget import LoopBudgetPolicy, default_role_budget_policy
from agent.extensions import ToolDisclosurePolicy, build_tool_search_capabilities
from agent.hooks import sort_capabilities
from agent.tools import LockMode


class RoleTicketIndex(Protocol):
    """任务级 Role 子会话索引协议（由 SQLModelTicketStore 实现）。"""

    async def get_role_conversation_id(self, main_conversation_id: str, task_id: str) -> str | None: ...

    async def register_role_conversation(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        role_name: str,
        role_conversation_id: str,
    ) -> None: ...


@dataclass(frozen=True, kw_only=True)
class RoleSpec:
    """无状态角色定义：封装 Harness 能力与指令，不绑定对话历史或工作目录。"""

    name: str
    description: str
    instructions: str
    model: Model | KnownModelName | str
    capabilities: Sequence[AbstractCapability[object]] = ()
    toolsets: Sequence[AbstractToolset[object]] = ()
    default_lock_mode: LockMode = "exclusive"
    tool_disclosure: ToolDisclosurePolicy | None = None
    budget_policy: LoopBudgetPolicy | None = None

    def effective_budget_policy(self) -> LoopBudgetPolicy:
        """返回角色显式配置的预算策略，或按角色职责/锁模式推导的默认策略。"""
        if self.budget_policy is not None:
            return self.budget_policy
        return default_role_budget_policy(self.name, default_lock_mode=self.default_lock_mode)

    def build_agent(
        self,
        *,
        step_store: StepStore | None = None,
        extra_capabilities: Sequence[AbstractCapability[object]] = (),
        extra_toolsets: Sequence[AbstractToolset[object]] = (),
        extra_instructions: str = "",
        model_override: Model | KnownModelName | str | None = None,
    ) -> Agent[object, str]:
        """按无状态蓝图构建单次运行可用的 Agent 实例。"""
        caps: list[AbstractCapability[object]] = [
            *self.capabilities,
            *extra_capabilities,
        ]
        if self.tool_disclosure is not None:
            caps.extend(build_tool_search_capabilities(self.tool_disclosure))
        if step_store is not None:
            caps.append(
                StepPersistence(store=step_store, agent_name=self.name, capture_frontier=True)
            )
        full_instructions = (
            f"{self.instructions}\n\n{extra_instructions}"
            if extra_instructions.strip()
            else self.instructions
        )
        policy = self.effective_budget_policy()
        effective_model = model_override if model_override is not None else self.model
        return Agent(
            model=effective_model,
            deps_type=object,
            name=self.name,
            instructions=full_instructions,
            toolsets=[*self.toolsets, *extra_toolsets],
            capabilities=sort_capabilities(caps),
            retries=policy.max_retries,
        )


class RoleRegistry:
    """Role Agent 蓝图注册表。"""

    def __init__(self, specs: Sequence[RoleSpec] = ()) -> None:
        self._roles: dict[str, RoleSpec] = {spec.name: spec for spec in specs}

    def register(self, spec: RoleSpec) -> None:
        self._roles[spec.name] = spec

    def get(self, role_name: str) -> RoleSpec:
        spec = self._roles.get(role_name)
        if spec is None:
            available = ", ".join(sorted(self._roles)) or "(无)"
            raise LookupError(f"未知角色 @{role_name}，可用角色: {available}")
        return spec

    def list_roles(self) -> list[RoleSpec]:
        return list(self._roles.values())


def build_default_role_registry(model: Model | KnownModelName | str) -> RoleRegistry:
    """基于 pydantic_ai_harness 原生能力构建默认 Role Agents 注册表。"""
    return RoleRegistry(
        [
            RoleSpec(
                name="coder",
                description="负责编码实现、文件修改与重构。",
                instructions=(
                    "你是专业软件工程师（coder）。严格在给定工作区目录内完成编码与修改任务，"
                    "遵循局部 AGENTS.md，完成后简明汇报修改的文件与验证结果。"
                ),
                model=model,
                capabilities=[Coder(workspace=None, sub_agents=False)],
                default_lock_mode="exclusive",
            ),
            RoleSpec(
                name="reviewer",
                description="只读代码审查专家，负责发现缺陷、边界问题与规范偏差。",
                instructions=(
                    "你是严格的代码审查员（reviewer）。使用只读工具检查改动，"
                    "按严重程度列出问题、涉及文件与具体修复建议，不直接修改文件。"
                ),
                model=model,
                capabilities=[FileSystem(read_only=True)],
                default_lock_mode="shared",
            ),
            RoleSpec(
                name="tester",
                description="负责运行测试命令、定位失败原因并验证回归。",
                instructions=(
                    "你是测试工程师（tester）。阅读相关测试与实现后执行测试命令，"
                    "准确报告通过/失败数量及关键错误栈，不擅自修改业务代码。"
                ),
                model=model,
                capabilities=[FileSystem(read_only=True), Shell()],
                default_lock_mode="exclusive",
            ),
            RoleSpec(
                name="researcher",
                description="负责代码库调研、文档检索与技术方案对比（只读共享锁，可与其它只读任务并发）。",
                instructions=(
                    "你是技术调研员（researcher）。广泛检索并核实证据，"
                    "清晰区分已验证事实与推断，输出结构化调研结论。"
                    "注意：当前工作区根目录即 '.'，查看目录请优先使用 '.'，不要假定 '.git' 等目录一定存在。"
                ),
                model=model,
                capabilities=[FileSystem(read_only=True)],
                default_lock_mode="shared",
            ),
        ]
    )


class RoleSessionManager:
    """按 (main_conversation_id, task_id) 管理任务专属的持久化 Context Window。"""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        ticket_index: RoleTicketIndex,
    ) -> None:
        self._conversations: ConversationStore = conversation_store
        self._tickets: RoleTicketIndex = ticket_index
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def load_task_conversation(
        self,
        *,
        main_conversation_id: str,
        task_id: str,
        role_name: str,
        role_instructions: str,
        inherit_history: bool = False,
    ) -> Conversation:
        """加载或初始化指定 @task_id 专属的 Role Context Window（Revise 时沿用原链）。"""
        lock = self._locks.setdefault((main_conversation_id, task_id), asyncio.Lock())
        async with lock:
            scope = f"conv:{main_conversation_id}"
            role_conv_id = await self._tickets.get_role_conversation_id(main_conversation_id, task_id)
            if role_conv_id is not None:
                loaded = await self._conversations.load(role_conv_id, scope=scope)
                if loaded is not None:
                    return loaded

            forked = await self._conversations.fork(
                main_conversation_id,
                at=None if inherit_history else 0,
                scope=scope,
                source_scope="main",
            )
            seed = ModelRequest(
                parts=[SystemPromptPart(content=f"[Task @{task_id} | Role: {role_name}]\n{role_instructions}")]
            )
            forked.messages.append(seed)
            _ = await self._conversations.save(forked, scope=scope)
            await self._tickets.register_role_conversation(
                main_conversation_id=main_conversation_id,
                task_id=task_id,
                role_name=role_name,
                role_conversation_id=forked.conversation_id,
            )
            return forked

    async def save_task_result(self, main_conversation_id: str, result: AgentRunResult[str]) -> None:
        """将任务增量原子推进到其专属 head_entry_id。"""
        scope = f"conv:{main_conversation_id}"
        try:
            _ = await self._conversations.save(result, scope=scope)
        except ConversationConflict:
            latest = await self._conversations.load(result.conversation.conversation_id, scope=scope)
            if latest is None:
                raise
            latest.messages.extend(result.new_messages())
            _ = await self._conversations.save(latest, scope=scope)
