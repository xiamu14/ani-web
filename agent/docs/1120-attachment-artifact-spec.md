# 附件引用传输（Attachment）、内部产物引用（Artifact）、大载荷外置压缩（OPT-3/4/6）与结果溯源设计规范（Spec）

- **基线 Commit**：`1723199`（`feat(llm): add Step 10.1-10.4 dual-pool LLM concurrency limiter, 429 cooldown gate, and Jev throttling`）
- **关联计划文档**：[1120-attachment-artifact-plan.md](./1120-attachment-artifact-plan.md)
- **关联历史优化清单**：[1117-optimization.md](./1117-optimization.md)（完整收编并实现 **OPT-3**、**OPT-4**、**OPT-6**）

---

## 一、 核心设计目标

1. **高性能内容寻址存储与写锁隔离（落地 OPT-3 & OPT-4）**：
   - **OPT-3（多媒体纯二进制外置与 `sha256` 去重）**：对图像（`image/*`）、音视频（`audio/*`, `video/*`）、PDF（`application/pdf`）等已压缩二进制载荷，超过阈值（默认 `64 KiB`）时由 `CompressedFileMediaStore` 外置为 `<media_dir>/<sha256>.bin` 纯二进制文件，**不做二次通用压缩**，消除 ~33% Base64 膨胀并在跨对话、`fork` 及多 Role 间 100% 物理去重。
   - **OPT-4（超大文本 / 工具日志快速压缩外置）**：对 $\ge 64\text{ KiB}$ 的超长文本、终端日志或大 JSON 载荷，使用 `zlib.compress(data, level=2)` 快速轻量压缩后外置存储（4x–10x+ 压缩比），采用 UUID 唯一临时文件原子落盘防并发竞态；仅当压缩后体积严格小于原文时落盘压缩版并在 sidecar `.meta.json` 标记 `codec="zlib"`，否则原样直存并标记 `codec="raw"`。
2. **窗口化按需还原附件与 SQL 下推裁剪（落地 OPT-6）**：
   - `SQLModelConversationStore.read_history(head_id, limit=N, media_window=K)` 在 SQL 递归 CTE 阶段结合 `stop_backtrack = 1` 与 `WHERE message_json IS NOT NULL LIMIT :limit` 下推裁剪；
   - **仅对最近 $K$ 条活跃消息（`media_window`，默认 4）执行完整二进制/大文本 `restore_media`**；对窗口外的旧历史轮次中的外部大附件，自动替换为紧凑文本占位符，避免上百轮对话重放时反复载入几十 MB 历史载荷。
3. **跨对话 / 跨 Role 的双轨全局引用与缓存（`AttachmentRegistry`：`attachment` & `artifact`）**：
   - **多类型文件附件引用（`AttachmentRef` / `att_<sha256[:12]>`）**：
     - 统一依托 `CompressedFileMediaStore` 读写字节（彻底消除与 `ConversationStore` 共用目录时的 `zlib` 压缩格式冲突）；
     - 跨对话、跨 Role Agent 引用同一资源时基于 SHA-256 映射到同一 `attachment_id`，共享底层去重存储、内存分页切片缓存以及上游 `UploadedFile(file_id, provider_name)` / `public_url`（完整覆盖 `ImageUrl`、`AudioUrl`、`VideoUrl`、`DocumentUrl`），实现零重复上传与 KV Cache 前缀稳定；
     - 紧凑句柄格式：`[[attachment:att_<sha256[:12]> | <title> (<media_type>, <size>)]]`。
   - **内部资源产物 / 状态变量快速引用（`ArtifactRef` / `art_<id>` 或命名 key）**：
     - 支持在工作流或 Multi-Agent 间通过内部资源 ID 快速传递结构化 JSON、分析产物或状态变量，支持点号路径按需读取（如 `pricing.min_charge`）；
     - 紧凑句柄格式：`[[artifact:<artifact_id> | json (<size>)]]`。
   - **工作区沙箱安全边界**：`register_attachment` 在绑定 `workspace_root` 时强制校验目标路径 `is_relative_to(workspace_root)`，防止路径穿越注册工作区外文件。
4. **生成结果的精准溯源引用与物理防伪核验（`citations.py`）**：
   - 统一支持 4 类溯源语法，并映射为 Pydantic AI 标准 `DocumentCitationSource` + `ContentCitationAnchor`：
     1. **文档页码与精确行范围**：`[[cite:att_<id>#page=4&line=12-18 | "摘录内容"]]`（指定 `line` 时严格在 `L12..L18` 行切片内校验 `excerpt`）
     2. **音视频时间轴区间**：`[[cite:att_<id>#t=01:15-01:42 | "关键帧或音频内容"]]`（校验 $0 \le t_1 \le t_2 \le \text{duration\_sec}$）
     3. **图像空间归一化区域（Bounding Box）**：`[[cite:att_<id>#bbox=0.15,0.20,0.85,0.45 | "目标区域描述"]]`（校验 $0 \le x_1 < x_2 \le 1, 0 \le y_1 < y_2 \le 1$）
     4. **内部产物 / 状态变量字段路径**：`[[cite:art:<id>#key=pricing.min_charge | "24.75"]]`（兼容 `state:` 别名）
   - **严格防伪**：对未知 fragment 键、非 `k=v` 畸形片段、越界页码/行号/时间点或虚构资源 ID 一律标记 `verified=False` 并返回明确原因。

---

## 二、 模块分层与强解耦拓扑

```text
Layer 0（纯引用模型与溯源防伪核验：零 DB、零网络）
├── attachments/ports.py      # AttachmentKind, AttachmentRef, ArtifactRef, CitationLocator, VerifiedCitation, AttachmentRegistryPort
└── attachments/citations.py  # parse_time_range_sec(), parse_bbox(), resolve_artifact_key_path(), extract_and_verify_citations()

Layer 1（统一 CAS 压缩存储、跨会话/跨 Role 缓存注册表与 OPT-3/4/6 ConversationStore）
├── db/media_store.py         # CompressedFileMediaStore（OPT-3 二进制直存 + OPT-4 zlib 压缩 + UUID 原子写 + OPT-6 占位符）
├── db/_conv_helpers.py       # dump_message_async(), load_message_async()
├── db/conversation_store.py  # SQLModelConversationStore（SQL LIMIT 下推 + media_window 窗口还原）
└── attachments/registry.py   # AttachmentRegistry（复用 CompressedFileMediaStore，统一管理 AttachmentRef & ArtifactRef）

Layer 2（按需检视工具集与 Runtime 装配）
├── attachments/toolset.py    # build_attachment_toolset()（register_attachment, inspect_attachment, list_attachments, save_artifact, get_artifact, verify_citations）
├── tools/tool_queue.py       # 读写锁分级（只读免锁并发、写操作排他串行）
├── core/scheduler.py         # WorkspaceLockManager 挂载 attachment_toolset 至 Role Agents
└── core/runtime.py           # open_runtime 统一装配 CompressedFileMediaStore 与 AttachmentRegistry
```
