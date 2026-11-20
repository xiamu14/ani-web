# 附件引用传输（Attachment）、内部产物引用（Artifact）与 OPT-3/4/6 实施计划（Plan）

- **基线 Commit**：`1723199`
- **对应设计规范**：[1120-attachment-artifact-spec.md](./1120-attachment-artifact-spec.md)
- **迁移任务来源**：收编 [1117-optimization.md](./1117-optimization.md) 中的 **OPT-3**、**OPT-4**、**OPT-6**
- **实施原则**：小步快走、强解耦、单文件 `< 500` 行、每一步均可独立测试验证。

---

## 一、 分步实施 TODO 清单（Step 11.1 – Step 11.4）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 11.1** | `attachments/ports.py`、`attachments/citations.py`、`attachments/registry.py` | **Layer 0/1 附件/产物引用模型、跨会话缓存注册表与严格溯源校验器**：定义 `AttachmentRef`、`ArtifactRef`、`CitationLocator`、`VerifiedCitation`；实现 `AttachmentRegistry`（复用 `CompressedFileMediaStore` 零冲突读写、SHA-256 去重、`UploadedFile` 与全模态 URL 含 `AudioUrl` 缓存、内部 `Artifact` 快速 ID 与点号路径读取）；实现 `extract_and_verify_citations()`（精确行区间 excerpt 核验 + 拒绝未知/畸形 fragment + 4 类物理边界校验） | `tests/test_attachments_and_artifacts.py` (Part 1: 去重缓存、`AudioUrl` 与严格溯源防伪单测) | `[x]` DONE |
| **Step 11.2** | `db/media_store.py`、`db/_conv_helpers.py`、`db/conversation_store.py` | **落地 OPT-3 + OPT-4 + OPT-6 存储层**：实现 `CompressedFileMediaStore`（多媒体纯二进制直存 + $\ge 64\text{KiB}$ 大文本 `zlib(level=2)` 快速压缩 + UUID 唯一临时文件原子落盘 + 精确 `codec` 元数据）；升级 `SQLModelConversationStore.read_history` 支持 SQL 级 `LIMIT` 下推与 `media_window` 窗口化按需还原 | `tests/test_attachments_and_artifacts.py` (Part 2: OPT-3 去重、OPT-4 压缩、OPT-6 SQL LIMIT + 窗口水合、与 `AttachmentRegistry` 交叉读写零冲突单测) | `[x]` DONE |
| **Step 11.3** | `attachments/toolset.py`、`tools/tool_queue.py`、`memory/runtime_adapter.py`、`core/scheduler.py`、`core/runtime.py`、`docs/1117-optimization.md` | **Layer 2 附件/产物工具集与跨 Role 运行时装配**：实现 `build_attachment_toolset(registry)`（`register_attachment` 含工作区沙箱校验、`inspect_attachment`、`list_attachments`、`save_artifact`、`get_artifact`、`verify_citations`）；在 `open_runtime` 与 `WorkspaceLockManager` 中装配 `CompressedFileMediaStore` 与 `AttachmentRegistry`；同步更新 `docs/1117-optimization.md` OPT-3/4/6 状态 | `tests/test_attachments_and_artifacts.py` (Part 3: 跨 Role 附件/产物句柄传递、越界路径拦截与溯源核验 E2E 测试) | `[x]` DONE |
| **Step 11.4** | 全量 `src/` 与 `tests/` | **全量回归、大文件压缩/窗口加载基准实测与代码质量门禁**：验证所有源文件 `< 500` 行、`basedpyright src/ tests/test_attachments_and_artifacts.py` (`0 errors`)、`ruff check src/ tests/` (`All checks passed!`)、全仓 96+ 测试通过并输出大文件压缩与窗口加载实测数据 | 全量 `unittest` 套件 + 性能基准断言 | `[x]` DONE |
