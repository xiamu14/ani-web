# 数据库与并发性能优化清单（TODO）

本文整理 [Context Tree 架构设计](./1117-conversation-tree.md)、[Super-Agent 架构设计](./1117-super-agent.md) 与 [重启恢复扩展设计](./1117-super-agent2.md) 在“**上百轮对话 + 数十个异步后台任务并发**”场景下的存储、内存与写锁优化点，并标记当前待办状态。

---

## 一、 优化点总览（TODO 状态）

| 编号 | 优化项 | 所属模块 | 优先级 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **OPT-1** | 正式运行强制使用本地文件库（`WAL` 模式），禁用 `":memory:"` | `db/engine.py` | P0 | `[x]` 已支持（默认走固定路径文件库） |
| **OPT-2** | `SQLModelConversationStore` 与 `SqliteStepStore` 分库存放，物理隔离写锁 | `db/` 运行时装配 | P0 | `[x]` 已完成（`context_tree.db` / `step_persistence.db`） |
| **OPT-3** | 接入 `FileMediaStore` + `externalize_media` / `restore_media`，多媒体纯二进制外置去重 | `db/conversation_store.py` | P0 | `[ ]` TODO |
| **OPT-4** | 超大文本 / 长日志快速轻量压缩（`zlib level=1~3` 或 `zstd`） | `db/conversation_store.py` | P1 | `[ ]` TODO |
| **OPT-5** | `_gc_unreachable_entries` 改为单条 SQL `WITH RECURSIVE` 或后台异步清理 | `db/_conv_helpers.py` | P1 | `[x]` 已完成 |
| **OPT-6** | 配合 `load(..., limit=N)` 与 `handoff(stop_backtrack=1)` 按需还原窗口附件 | `db/conversation_store.py` / `core/` | P2 | `[ ]` TODO |

---

## 二、 各项优化方案与实施说明

### OPT-1：正式运行使用本地文件库（`WAL` 模式） `[x]`
* **现状**：`create_sqlite_async_engine(None)` 默认走固定本地文件库并开启 `PRAGMA journal_mode = WAL`、`PRAGMA synchronous = NORMAL` 与 `PRAGMA busy_timeout = 5000`；`":memory:"` 仅用于单元测试。
* **原则**：业务运行时不传 `":memory:"`，确保崩溃可恢复且避免连接池孤立。

### OPT-2：`context_tree.db` 与 `step_persistence.db` 分库存放 `[x]`
* **现状**：`open_runtime` 与 `default_step_db_path()` 已将 `SQLModelConversationStore` / `SQLModelTicketStore`（`context_tree.db`）与 `SqliteStepStore`（`step_persistence.db`）物理分库存放，彻底隔离写锁竞争。

### OPT-3：多媒体纯二进制外置与 `sha256` 去重 `[ ] TODO`
* **问题**：直接在 `context_entries.message_json` 内联图片、音频、PDF 等 `BinaryContent` 会带来 ~33% 的 Base64 膨胀，放大内存占用与 SQLite 写锁时长；同时对已压缩多媒体格式（JPEG/PNG/WebP/MP3/MP4/PDF）做通用二次压缩几乎无收益，反而消耗 CPU。
* **方案**：
  * 复用 `pydantic_ai_harness.step_persistence._store` 内置的 `FileMediaStore`、`externalize_media` 与 `restore_media`。
  * 在 `SQLModelConversationStore` 的 `_dump_message` / `_load_message` 边界，将超过阈值（默认 `64 KiB`）的 `BinaryContent` 抽离为基于 `sha256` 内容寻址的纯二进制文件，仅在 `message_json` 中保留轻量 URI 引用。
  * **不做二次通用压缩**，保留原始二进制文件以支持快速读取与零拷贝传输，且在 `fork` 与多任务间天然去重。

### OPT-4：超大文本 / 工具日志快速压缩 `[ ] TODO`
* **问题**：超长命令输出、终端日志或大代码快照会显著撑大 `context_entries.message_json` 与内存中的 Pydantic 对象。
* **方案**：对超过阈值（如 `64 KiB`）的纯文本/JSON 载荷，采用标准库 `zlib.compress(..., level=1~3)` 或 `zstd` 进行快速轻量压缩后外置存储（4x–10x 压缩比，微秒级 CPU 开销）；短消息（`< 64 KiB`）保持明文直存，不增加延迟。

### OPT-5：优化 `delete_record` 中的孤立节点 GC `[x]`
* **现状**：`src/agent/db/_conv_helpers.py` 中的 `gc_unreachable_entries` 已改为单条 SQL `WITH RECURSIVE ... DELETE FROM context_entries` 批量计算可达集并清理孤立非 root 节点，消除 N+1 查删。

### OPT-6：窗口化按需还原附件 `[ ] TODO`
* **方案**：加载上下文时结合 `ConversationStore.load(..., limit=N)` 与 `handoff` 节点的 `stop_backtrack = 1` 截断边界，仅对实际进入本次 LLM Context Window 的最近 N 条消息执行 `restore_media`，避免把上百轮历史附件一次性读入内存。
