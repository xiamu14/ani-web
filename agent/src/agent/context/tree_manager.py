"""Context Tree 生命周期管理器: Root 创世、Workspace 绑定、分支派生与重启恢复。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    SystemPromptPart,
    repair_messages,
)
from pydantic_ai_harness.step_persistence import StepStore

from agent.context.instructions import (
    load_global_instructions,
    normalize_workspace_path,
    read_agents_file,
)
from agent.db import LAST_ACTIVE_CONV_KEY, SQLModelConversationStore
from agent.memory import MemoryStorePort, build_l0_compaction_recovery_block

GLOBAL_INSTRUCTIONS_KEY = "global_instructions_loaded"


@dataclass(frozen=True, kw_only=True)
class WorkspaceBinding:
    """工作区绑定结果，包含规范化路径及该目录自身的 AGENTS.md 内容。"""

    conversation_id: str
    work_path: str
    agents_md: str | None


@dataclass(frozen=True, kw_only=True)
class BranchInfo:
    """主会话分支轻量索引，供显式切换与语义路由使用。"""

    conversation_id: str
    target_title: str
    work_path: str | None
    status: str
    updated_at: str


class ContextTreeManager:
    """管理主对话 Context Tree 生命周期（与 Router 和后台调度解耦）。"""

    def __init__(
        self,
        store: SQLModelConversationStore,
        *,
        step_store: StepStore | None = None,
        memory_store: MemoryStorePort | None = None,
    ) -> None:
        self._store: SQLModelConversationStore = store
        self._step_store: StepStore | None = step_store
        self._memory_store: MemoryStorePort | None = memory_store
        self._root_entry_id: str | None = None
        self._active_conversation_id: str | None = None

    @property
    def active_conversation_id(self) -> str | None:
        return self._active_conversation_id

    @property
    def root_entry_id(self) -> str | None:
        return self._root_entry_id

    async def bootstrap(self, *, global_agents_path: str | Path | None = None) -> str | None:
        """应用启动初始化：幂等建立 Root Node，载入全局设定，并返回上次退出的主 conversation_id。"""
        self._root_entry_id = await self._store.ensure_root_node()
        global_text = load_global_instructions(global_agents_path)
        if global_text is not None:
            await self._store.set_app_state(GLOBAL_INSTRUCTIONS_KEY, global_text)
        last_conv_id = await self._store.get_last_active_conversation_id()
        self._active_conversation_id = last_conv_id
        if last_conv_id is not None and self._step_store is not None:
            _ = await self.repair_interrupted_conversation(last_conv_id)
        return last_conv_id

    async def repair_interrupted_conversation(self, conversation_id: str) -> bool:
        """若主会话存在未闭合的 StepPersistence frontier 快照，使用 repair_messages 修补并落盘。"""
        if self._step_store is None:
            return False
        runs = await self._step_store.list_runs(conversation_id=conversation_id)
        if not runs:
            return False
        snapshot = await self._step_store.latest_snapshot(
            run_id=runs[-1].run_id, include_interrupted=True
        )
        if snapshot is None:
            return False
        conv = await self._store.load(conversation_id, scope="main")
        if conv is None:
            return False
        repaired: list[ModelMessage] = repair_messages(snapshot.messages)
        if len(repaired) <= len(conv.messages):
            return False
        merged = Conversation(
            messages=repaired,
            usage=conv.usage,
            conversation_id=conv.conversation_id,
            deferred_tool_requests=conv.deferred_tool_requests,
            revision=conv.revision,
        )
        try:
            _ = await self._store.save(merged, scope="main")
        except ConversationConflict:
            return False
        return True

    async def ensure_active_conversation(
        self,
        *,
        work_path: str | Path | None = None,
        target_title: str = "",
    ) -> Conversation:
        """获取当前活跃主会话；若尚无会话则以 E_root 为父节点创建首条会话。"""
        if self._root_entry_id is None:
            _ = await self.bootstrap()

        if self._active_conversation_id is not None:
            return await self.load_conversation(self._active_conversation_id)

        norm_path = str(normalize_workspace_path(work_path)) if work_path is not None else None
        record = await self._store.create_conversation_from_root(
            work_path=norm_path,
            scope="main",
            target_title=target_title,
        )
        self._active_conversation_id = record.conversation_id
        conv = await self.load_conversation(record.conversation_id)

        global_text = await self._store.get_app_state(GLOBAL_INSTRUCTIONS_KEY)
        if global_text is not None:
            conv.messages.append(ModelRequest(parts=[SystemPromptPart(content=global_text)]))
            _ = await self._store.save(conv, scope="main")
            conv = await self.load_conversation(record.conversation_id)

        if norm_path is not None:
            _ = read_agents_file(Path(norm_path))
        return conv

    async def load_conversation(self, conversation_id: str, *, scope: str = "main") -> Conversation:
        """加载指定会话完整上下文链；读取失败时明确报错，不静默返回空历史。"""
        conv = await self._store.load(conversation_id, scope=scope)
        if conv is None:
            raise LookupError(f"会话 {conversation_id!r} 不存在或 scope 不匹配。")
        return conv

    async def bind_workspace(self, conversation_id: str, path: str | Path) -> WorkspaceBinding:
        """原地更新指定会话的工作区目录（与分支切换解耦；AGENTS.md 仅在请求末尾动态注入）。"""
        directory = normalize_workspace_path(path)
        norm_path = str(directory)
        await self._store.bind_workspace(conversation_id, norm_path)
        agents_md = read_agents_file(directory)
        return WorkspaceBinding(conversation_id=conversation_id, work_path=norm_path, agents_md=agents_md)

    async def get_active_workspace_prompt(self, conversation_id: str) -> str | None:
        """读取当前激活工作区自身的 AGENTS.md，用于每次请求末尾动态注入（不持久化进历史链）。"""
        work_path = await self.get_work_path(conversation_id)
        if work_path is None:
            return None
        agents_md = read_agents_file(Path(work_path))
        if agents_md is None:
            return None
        return f"[Workspace Active: {work_path}]\n{agents_md}"

    async def switch_conversation(self, conversation_id: str) -> Conversation:
        """仅切换到已存在的主会话并持久化到 app_state，绝不自动新建。"""
        conv = await self.load_conversation(conversation_id)
        await self._store.set_app_state(LAST_ACTIVE_CONV_KEY, conversation_id)
        self._active_conversation_id = conversation_id
        return conv

    async def fork_handoff(
        self,
        parent_conversation_id: str,
        *,
        summary: str,
        work_path: str | Path | None = None,
        target_title: str = "",
    ) -> Conversation:
        """在当前分支末端插入 stop_backtrack=1 交接节点，派生新主分支并切换焦点（自动附带 L0 记忆状态）。"""
        norm_path = str(normalize_workspace_path(work_path)) if work_path is not None else None
        effective_path = norm_path or await self._store.get_work_path(parent_conversation_id)
        handoff_text = f"[Handoff Context]\n{summary}"
        if self._memory_store is not None:
            l0_block = await build_l0_compaction_recovery_block(
                self._memory_store,
                work_path=effective_path,
            )
            if l0_block:
                handoff_text = f"{handoff_text}\n\n{l0_block}"
        handoff_msg = ModelRequest(parts=[SystemPromptPart(content=handoff_text)])
        record = await self._store.create_handoff_branch(
            parent_conversation_id,
            handoff_message=handoff_msg,
            work_path=norm_path,
            target_title=target_title,
            scope="main",
        )
        self._active_conversation_id = record.conversation_id
        return await self.load_conversation(record.conversation_id)

    async def get_work_path(self, conversation_id: str) -> str | None:
        return await self._store.get_work_path(conversation_id)

    async def list_branches(self, *, limit: int = 20) -> list[BranchInfo]:
        """按最近更新时间倒序列出主会话分支索引。"""
        rows = await self._store.list_main_branches(limit=limit)
        return [
            BranchInfo(
                conversation_id=row.conversation_id,
                target_title=row.target_title,
                work_path=row.work_path,
                status=row.status,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def create_root_branch(
        self,
        *,
        target_title: str = "",
        work_path: str | Path | None = None,
    ) -> Conversation:
        """从 E_root 创建一条全新主分支并切换活跃指针。"""
        if self._root_entry_id is None:
            _ = await self.bootstrap()
        norm_path = str(normalize_workspace_path(work_path)) if work_path is not None else None
        record = await self._store.create_conversation_from_root(
            work_path=norm_path,
            scope="main",
            target_title=target_title,
        )
        self._active_conversation_id = record.conversation_id
        conv = await self.load_conversation(record.conversation_id)
        global_text = await self._store.get_app_state(GLOBAL_INSTRUCTIONS_KEY)
        if global_text is not None:
            conv.messages.append(ModelRequest(parts=[SystemPromptPart(content=global_text)]))
            _ = await self._store.save(conv, scope="main")
            conv = await self.load_conversation(record.conversation_id)
        return conv

    async def update_target_title(self, conversation_id: str, target_title: str) -> None:
        await self._store.update_target_title(conversation_id, target_title)
