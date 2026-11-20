# 基于 Pydantic AI 原生架构的 Memory 代码层优化与结构简化计划（Step 8.1 – 8.4）

- **基线 Commit**：`a99e45e`（`test(memory): add Step 7.5 dual-axis fidelity and ROI evaluation benchmark`）
- **基线验证状态**：`tests/test_memory_*.py` 共 **18 项测试 100% 通过**，`tests/e2e_memory_eval.py` 双轴 8 项指标 100% 达标，`basedpyright` `0 errors`，`ruff check` `0 errors`。
- **重构目标**：在 **100% 保持现有功能、公开 API 向后兼容与 18 项单测 + 双轴评测零退化** 的前提下，基于 Pydantic AI (`v2.69.0`) 原生 `AbstractCapability` 生命周期消除重复代码、收敛跨层胶水并消除重复写入与串行查询开销。

---

## 一、 现状问题与架构优化设计

### 1. 磁盘路径解析与行锚点批量核验的 DRY 收敛（Step 8.1）
- **现状问题**：
  - “将工作区相对/绝对路径解析为 `(rel_path, disk_path)` 并安全读取磁盘文件核验 `ObservationRecord.line_anchors`”在 `memory/runtime_adapter.py`、`memory/toolset.py`、`hooks/memory_hook.py`、`memory/extractor.py` **4 处各自实现了一遍**；
  - `memory/toolset.py` 的 `_verify_obs_against_disk()` 在 `memory_search` / `memory_timeline` 循环渲染 15 条命中同一文件的观察时，缺少文件级缓存，导致同一文件被重复读取与构建 `FileLineIndex` 15 次（$O(K)$ 磁盘 IO）。
- **优化设计**：
  - 在 `memory/anchors.py` 中保持纯算法零 IO 属性，新增 `verify_line_anchors_with_index(anchors, target_path, index)` 以支持复用已构建的 `FileLineIndex`；
  - 在 `memory/workspace_io.py`（轻量级工作区文件与锚点核验辅助模块，约 80 行）集中提供：
    1. `resolve_workspace_path(raw_path, workspace_root) -> tuple[str, Path | None]`
    2. `read_workspace_text(raw_path, workspace_root) -> str | None`
    3. `verify_observations_on_disk(observations, workspace_root) -> dict[int, LineAnchorVerification]`（内部按 `rel_path` 缓存文件内容与 `FileLineIndex`，同一文件仅读盘 + 建索引 1 次）。

### 2. L2 `edit_file` 实时写穿记录与任务完成 `TrajectoryMemoryExtractor` 的合流去重（Step 8.2）
- **现状问题**：
  - 当 Role 在一次任务中调用 `edit_file("tariffs.py", ...)` 时，`FileMemoryContextHook._handle_write_file` 会实时写入一条机械观察 `title="Modified tariffs.py via edit_file"`；
  - 随后在任务完成时，`TrajectoryMemoryExtractor.extract_and_store()` 又从 `messages` 提取同一次 `edit_file` 并写入一条带任务语义的观察，导致单次文件修改在同 `conversation_id` 下残留一条机械的 `"Modified <file> via <tool>"` 冗余记录。
- **优化设计**：
  - 在 `TrajectoryMemoryExtractor.extract_and_store()` 落库语义化任务级 `Observation` 后，对本次任务 `files_modified` 下同 `conversation_id` 产生的机械临时记录（`title` 形如 `"Modified <rel_p> via "` 且 `id != saved_obs.record.id`）自动调用 `mark_superseded(..., superseded_by=saved_obs.record.id)`；
  - 既保留了工具执行期 `FileMemoryContextHook` 实时写穿失效与单步可观测性（Step 7.3 单测完全兼容），又保证完整 Role 任务结束后 L1 活跃记忆窗口中只保留高语义密度的任务级观察。

### 3. 基于 Pydantic AI 原生 `AbstractCapability` 的生命周期统一与并发查询优化（Step 8.3）
- **现状问题**：
  - `build_l0_l1_memory_block()` 顺序串行等待 3 次 SQLite 查询（`list_recent_observations`、`list_recent_summaries`、`list_work_states`）；
  - `WorkspaceLockManager` 需要手工管理 `RoleMemoryBundle` 的 `extra_instructions`、`extra_toolsets`、`capabilities` 以及 `agent.run()` 后的 `archive_completed_role_task`。
- **优化设计**：
  - 在 `build_l0_l1_memory_block()` 与 `build_l0_compaction_recovery_block()` 中使用 `asyncio.gather()` 并发执行 L0/L1 查询；
  - 在 `FileMemoryContextHook`（或统一记忆 Capability）上原生实现 Pydantic AI `AbstractCapability` 的：
    - `get_instructions()`：支持直接承载预渲染或动态 L0+L1 指令块；
    - `get_toolset()`：原生返回 L3 `build_memory_toolset(...)`，使单个 Capability 同时具备 L0+L1 指令、L2 文件 Hook 与 L3 Toolset；
  - 同时保留 `RoleMemoryBundle` 与 `prepare_role_memory_bundle()` 的向后兼容接口（内部直接基于该原生 Capability 组装），确保现有 Step 7.4/7.5 测试零改动通过。

### 4. 全量回归测试、性能复测与代码质量门禁（Step 8.4）
- 运行全部 18 项单元测试、双轴评测基准 `tests/e2e_memory_eval.py`、500 条规模读写延迟基准、`basedpyright` 严格类型检查、`ruff check` 与全文件 `< 500` 行检查。

---

## 二、 分步实施 TODO 清单（小步快走，单步验证）

| 步骤 | 目标模块 | 核心改造内容 | 分步验证命令 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 8.1** | `memory/anchors.py`、`memory/workspace_io.py`、`memory/toolset.py`、`memory/runtime_adapter.py`、`hooks/memory_hook.py`、`memory/extractor.py` | **磁盘路径解析与行锚点批量核验 DRY 收敛**：新增 `verify_line_anchors_with_index()` 与 `memory/workspace_io.py`（带 `FileLineIndex` 缓存的 $O(1)$ 批量核验），替换 4 处重复实现 | `.venv/bin/python -m unittest tests/test_memory_pure_algorithms.py tests/test_memory_hooks_and_tools.py -v` | `[x]` DONE |
| **Step 8.2** | `memory/extractor.py`、`tests/test_memory_hooks_and_tools.py` | **工具期机械编辑记录与任务级语义观察合流**：`TrajectoryMemoryExtractor` 归档任务级观察后自动将同会话同文件的机械 `"Modified <path> via <tool>"` 临时观察标记为 `superseded_by` 最新语义观察 | `.venv/bin/python -m unittest tests/test_memory_hooks_and_tools.py tests/test_memory_runtime_integration.py -v` | `[x]` DONE |
| **Step 8.3** | `hooks/memory_hook.py`、`memory/runtime_adapter.py`、`core/scheduler.py` | **Pydantic AI 原生 `AbstractCapability` 统一挂载与 `asyncio.gather` 并发查询**：`FileMemoryContextHook` 原生实现 `get_instructions()` 与 `get_toolset()`；`build_l0_l1_memory_block` 使用 `asyncio.gather` 并发拉取 L0/L1 数据 | `.venv/bin/python -m unittest tests/test_memory_runtime_integration.py tests/test_memory_fidelity_and_roi.py -v` | `[x]` DONE |
| **Step 8.4** | 全量 `src/agent/memory`、`src/agent/db`、`src/agent/hooks`、`tests/` | **全量回归、双轴评测与性能复核**：验证 18+ 项单测、双轴评测基准、`basedpyright` (`0 errors`)、`ruff` (`0 errors`) 及所有源文件 `< 500` 行 | `.venv/bin/python -m unittest discover -s tests -p "test_memory*.py" -v && .venv/bin/python -m tests.e2e_memory_eval` | `[x]` DONE |
