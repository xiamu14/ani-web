# 同进程分层记忆（L0–L3 渐进披露）、物理锚点防错与双轴评测实施计划（Plan）

本文依据 [同进程分层记忆与双轴评测设计规范](./1120-memory-spec.md)，按“小步快走、强解耦、单文件 `< 500` 行、单步可独立测试验证”原则拆分为 5 个增量步骤（**Step 7.1 – Step 7.5**）。

---

## 一、 分步实施清单（TODO 状态）

| 步骤 | 目标模块 | 核心交付内容 | 独立验收测试 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Step 7.1** | `memory/ports.py`、`memory/anchors.py`、`memory/dedup.py`、`memory/rank.py`、`memory/budget.py` | **Layer 0 纯算法与契约层（零 IO）**：定义 4 类记忆模型与 `MemoryStorePort`；实现 `60/30/10` 显式省略截断、从真实工具报文提取唯一 `LineAnchor` 与实时重定位（`@L` / `(some code changed since)` / `(code changed since)`）、Tier-0 精确去重 + Tier-1 `tfidf_cosine` & `idfVeto` 稀有词否决、ACT-R 强化衰减排序（含 `recencyHeadSize` 最新 1/4 保护）、`WorkState` 折叠与 L0+L1 预算渲染 | `tests/test_memory_pure_algorithms.py` | `[x]` DONE |
| **Step 7.2** | `db/schema.py`、`db/models.py`、`db/memory_store.py` | **Layer 1 SQLite + FTS5 持久化层（无独立 Worker / 无 HTTP）**：新增 `observations`、`observations_fts`（FTS5 全文索引）、`session_summaries`、`work_states` 表；实现 `SQLModelMemoryStore`（支持 Tier-0 自动合并追加 `reinforcement_dates`、写穿标记 `superseded_by`、按项目/文件/概念/FTS5 检索及前后 `timeline` 邻域查询） | `tests/test_memory_store.py` | `[x]` DONE |
| **Step 7.3** | `memory/toolset.py`、`hooks/memory_hook.py`、`memory/extractor.py` | **L2 文件触发钩子、L3 三步检索工具集与写穿失效**：实现 `build_memory_toolset()`（`memory_search` → `memory_timeline` → `get_observations` + `save_memory`）；实现 `FileMemoryContextHook`（`read_file` $\ge 1.5\text{KB}$ 时注入该文件历史时间线 + 实时重算 `@L` 锚点，$\ge 32\text{KB}$ 大文件局部读取引导，单会话单文件去重；`edit_file`/`write_file` 后自动将失配旧锚点标记 `stale`/`superseded`）；实现任务完成时的轨迹提炼器 | `tests/test_memory_hooks_and_tools.py` | `[x]` DONE |
| **Step 7.4** | `core/scheduler.py`、`core/runtime.py`、`context/tree_manager.py`、`memory/runtime_adapter.py` | **运行时装配与生命周期闭环**：在 `open_runtime` 装配 `SQLModelMemoryStore`；Role Worker 启动时按 `work_path` 注入 L0+L1（对 `branch_mode == "full_fork"` 自动跳过防重复）；`Router` 与 `Role` 间 Handoff / Compaction 后恢复 L0 状态；Role 任务完成时自动归档 `Observation` 与 `SessionSummary` | `tests/test_memory_runtime_integration.py` | `[x]` DONE |
| **Step 7.5** | `tests/test_memory_fidelity_and_roi.py`、`tests/e2e_memory_eval.py` | **双轴评测基准（抗毒化正确性 + 减少重复工作 ROI）**：实现 **Arm OFF vs Arm ON-Fresh vs Arm ON-Stale** 双轴对照评测套件，覆盖 4 类防毒化陷阱（过期数值陷阱、行号漂移/删除陷阱、冲突决策覆盖、提取零幻觉审计）与 4 类减重 ROI 场景（`>32KB` 大文件跨任务定位修改、Gotcha 跨会话免踩坑、多 Role 接力零重复探索、`<1.5KB` 小文件免打扰校验） | `tests/test_memory_fidelity_and_roi.py` & `tests/e2e_memory_eval.py` | `[x]` DONE |

---

## 二、 各步可测试验证标准

* **Step 7.1（纯算法层验证）**：
  * **行锚点防错**：当文件上方插入 200 行或缩进变化时，`verify_line_anchors()` 100% 精确重定位到新行号 `@L<new>`；当目标代码行被修改或删除时，100% 返回 `changed`（标注 `(code changed since)`），绝不猜测行号；
  * **去重与 `idfVeto`**：完全相同的观察触发 Tier-0 合并；标题相似但含不同稀有函数名/错误码的观察被 `idfVeto` 100% 拦截不误合并；
  * **ACT-R 排序**：最新 `1/4` 窗口始终保持最新观察不被挤占；旧观察在跨天多次强化后激活度得分高于一次性普通旧观察。
* **Step 7.2（SQLite + FTS5 仓储验证）**：
  * 同进程异步写入重复观察时，表行数不增加，仅更新原记录 `reinforcement_dates_json` 与关联文件集合；
  * FTS5 `memory_search`、按 `files_modified` 特异性排序、`memory_timeline(anchor_id, depth_before, depth_after)` 及 `fold_work_state_list()`（`done` 状态自动隐藏、`null` 字段清除）全部通过断言。
* **Step 7.3（L2 文件钩子与 L3 检索工具验证）**：
  * `< 1.5KB` 小文件读取时不注入任何时间线（零额外 Token 开销）；
  * $\ge 1.5\text{KB}$ 文件首次 `read_file` 时注入带实时 `@L` 状态的紧凑时间线，同一会话第二次读取同一文件不再重复注入；
  * 对某文件执行 `edit_file` 修改锚定行后，再次查询该文件记忆时旧观察已被实时标记为 `(code changed since)` / `superseded`。
* **Step 7.4（运行时装配与 Fork 隔离验证）**：
  * `WorkspaceLockManager` 启动独立 Role 任务时自动获注 L0+L1 记忆并挂载 `MemoryToolset`；`full_fork` 分支不重复注入 L0+L1；
  * Role 任务完成后自动沉淀结构化 `Observation`（含真实工具提取的 `files_read`/`files_modified`/`line_anchors`）与 `SessionSummary`。
* **Step 7.5（双轴评测基准：抗毒化 Fidelity + 减重 ROI）**：
  * **抗毒化红线（100% 通过）**：在 `stale-value-trap`（记忆写 `22.40`，真实代码为 `24.75`）与 `anchor-drift-trap` 下，Agent 100% 通过锚点核验真实代码并输出 `24.75`，不被过期记忆误导；在 `superseded-decision-trap` 下 100% 采纳最新决策；
  * **减少重复工作 ROI**：在 `>32KB` 大文件定位修改与跨 Role 接力场景下，Arm ON-Fresh 相比 Arm OFF 整文件重复读取与全库重复搜索次数减少 $\ge 50\%$，跨会话已知 `gotcha` 试错失败次数降为 `0`。
