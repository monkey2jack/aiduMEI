# aiduMEM v10 "Synapse" 融合升级设计文档
>
> **副标题**：玄铁×aiduMEM 交叉授粉 — CoreMemory · Checkpoint · AutoDream
>
> 版本：v10.0.0-Synapse · 2026-07-27
> 设计师：AI（V4 Pro 大脑）
> 前置版本：v9.3.1-Aletheia

---

## 〇、升级策略

```
v9.3.1 Aletheia → v10.0.0 Synapse
                        ├─ §1 CoreMemory（LLM 可编辑 3-block）
                        ├─ §2 Checkpoint（5 段会话快照）
                        └─ §3 AutoDream（7 天自动蒸馏）
```

**安全原则**：
- 不删不改现有 API 路由，只新增
- 不改 facts.db 表结构，新功能用独立 SQLite 表
- 不中断 aiduMEM 服务（8767），热加载新模块
- 每个模块独立实现、独立测试、再合入

---

## 一、CoreMemory（LLM 可编辑结构化记忆块）

### 1.1 是什么

让 LLM（AI）自己维护 3 个结构化 block，替代现在散落在 MEMORY.md 里的平铺文本。

### 1.2 三个 Block

| Block | 键名 | 说明 | 预估体积 |
|-------|------|------|:---:|
| user_profile | `core_user_profile` | user是谁（身份、角色、偏好） | ~300 chars |
| current_project | `core_current_project` | 当前在做什么项目 | ~200 chars |
| key_decisions | `core_key_decisions` | 本次会话的关键决策 | ~300 chars |

### 1.3 存储

新增 `core_memory` 表（facts.db 内）：

```sql
CREATE TABLE core_memory (
    block_key   TEXT PRIMARY KEY,       -- core_user_profile / core_current_project / core_key_decisions
    content     TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 1.4 API

```
GET  /api/core-memory                    → 返回 3 个 block 的内容
PUT  /api/core-memory/{block_key}        → LLM 更新某个 block
POST /api/core-memory/inject             → 返回注入上下文的格式化文本
```

### 1.5 注入链路

```
mem0-inject.sh
  → /api/core-memory/inject
    → 拼成:
      [CoreMemory]
      👤 user: ...
      📋 当前项目: ...
      🔑 关键决策: ...
    → 拼在现有 [aiduMEM-v8 记忆注入] 前面
```

### 1.6 从现有 MEMORY.md 初始化

首次运行时，从 system prompt 中的 MEMORY 和 USER PROFILE 块提取初始值：

- `user_profile` ← 宿主 USER PROFILE 中的身份摘要（姓名/角色/偏好等，由部署方自行填充）
- `current_project` ← MEMORY 中的"三核cron" + "aidu品牌"
- `key_decisions` ← 宿主 MEMORY 中的硬约束条目（部署方自己的操作红线与既定规范）

---

## 二、Checkpoint（5 段会话快照）

### 2.1 是什么

每次上下文压缩时自动生成 5 段结构化快照，下次会话启动时注入，替代 session_search 全文检索的低效恢复。

### 2.2 五段快照

| 段 | 键名 | 说明 | 预估体积 |
|----|------|------|:---:|
| Active Intent | `cp_active_intent` | 当前在做什么任务 | ~100 chars |
| Next Action | `cp_next_action` | 下一步做什么 | ~100 chars |
| Current Work | `cp_current_work` | 最近修改的文件/状态 | ~200 chars |
| Key Decisions | `cp_key_decisions` | 本次会话的关键决策 | ~200 chars |
| Open Notes | `cp_open_notes` | 未完成/待跟进事项 | ~100 chars |

### 2.3 存储

新增 `checkpoints` 表（facts.db 内）：

```sql
CREATE TABLE checkpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    block_key   TEXT NOT NULL,           -- cp_active_intent / cp_next_action / ...
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_checkpoints_session ON checkpoints(session_id);
```

只保留最近 5 个会话的快照，旧快照自动清理。

### 2.4 API

```
GET  /api/checkpoint/latest              → 返回最近一次会话的 5 段快照
GET  /api/checkpoint/{session_id}        → 返回指定会话的快照
POST /api/checkpoint                     → 写入快照（由 Hermes 压缩 hook 触发）
DELETE /api/checkpoint/cleanup           → 清理 5 个会话之前的旧快照
```

### 2.5 触发时机

在 `mem0-inject.sh` 中增加检测逻辑：如果 Hermes payload 中 `is_compression_summary` 为 true，则调用 `/api/checkpoint` 生成快照。

### 2.6 注入链路

```
mem0-inject.sh (新会话首轮)
  → /api/checkpoint/latest
    → 拼成:
      [Checkpoint · 上次会话]
      🎯 在做: ...
      ⏭️ 下一步: ...
      📁 工作区: ...
      🔑 决策: ...
      📝 待办: ...
    → 拼在 [CoreMemory] 之后
```

---

## 三、AutoDream（7 天自动蒸馏）

### 3.1 是什么

定期自动蒸馏记忆：把多条相关事实合并、把过时信息标记为 superseded、把操作日志提炼为知识。

### 3.2 蒸馏流程

```
每 7 天触发一次（定时器）
  → Step 1: 扫描 facts.db，找出 7 天内新增的事实
  → Step 2: 聚类（相同 fact_key 前缀 / 同一 Lane）
  → Step 3: LLM 蒸馏（合并重复、提炼知识、标记过时）
  → Step 4: 写入 knowledge_evolution 关系 + 更新 memory_states
  → Step 5: 生成 AutoDream 报告
```

### 3.3 API

```
GET  /api/autodream/status               → 返回上次蒸馏时间和统计
POST /api/autodream/trigger              → 手动触发蒸馏
GET  /api/autodream/report               → 返回最近一次蒸馏报告
```

### 3.4 蒸馏 Prompt 模板

```python
AUTODREAM_PROMPT = """
你是记忆蒸馏器。以下是过去 7 天新增的事实列表。
请执行：
1. 合并：2 条以上描述同一事物的事实合并为 1 条
2. 提炼：操作日志（"改了 X 文件第 Y 行"）提炼为知识（"X 已优化过"）
3. 标记：已被新事实取代的旧事实标记为 superseded

返回 JSON：
{
  "merges": [{"source_ids": [1,2], "new_fact": "合并后的事实"}],
  "refinements": [{"source_id": 3, "new_fact": "提炼后的事实"}],
  "supersedes": [{"source_id": 4, "superseded_by_id": 5, "reason": "..."}]
}
"""
```

### 3.5 定时器

在 aiduMEM api_server.py 中新增一个后台线程，每 7 天触发一次。首次启动后 7 天开始计时。

---

## 四、版本升级路径

### 4.1 版本号

```
v9.3.1-Aletheia → v10.0.0-Synapse
```

### 4.2 命名

- 代号：**Synapse**（突触）— 象征记忆的连接、重组和高效传递
- 神谱定位：Aletheia（真理/揭示）→ Synapse（连接/整合）

### 4.3 文件变更清单

| 文件 | 变更 |
|------|------|
| `api_server.py` | 新增 3 组路由 + 后台 AutoDream 线程 + 版本号升级 |
| `ducky/core_memory.py` | **新建** — CoreMemory CRUD + 注入 |
| `ducky/checkpoint.py` | **新建** — Checkpoint 读写 + 清理 |
| `ducky/autodream.py` | **新建** — AutoDream 蒸馏引擎 |
| `mem0-inject.sh` | 增加 CoreMemory + Checkpoint 注入 |
| `ARCHITECTURE.md` | 更新架构图 |
| `CHANGELOG.md` | 追加 v10.0.0 |
| `aiduMEM-v10-Synapse-Whitepaper.md` | **新建** |

### 4.4 升级顺序

```
1. ducky/core_memory.py   → 独立测试
2. ducky/checkpoint.py    → 独立测试
3. ducky/autodream.py     → 独立测试
4. api_server.py          → 整合路由 + 升级版本号
5. mem0-inject.sh         → 更新注入链路
6. 集成测试               → 全链路验证
7. 白皮书 + 架构文档     → 收尾
```

---

## 五、风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|:---:|:---:|------|
| CoreMemory 与现有 MEMORY.md 内容冲突 | 低 | 中 | 首次从 MEMORY.md 初始化，后续 LLM 自行维护 |
| Checkpoint 生成质量差（LLM 敷衍） | 中 | 低 | prompt 模板加约束，最小长度校验 |
| AutoDream 误删重要记忆 | 低 | 高 | 蒸馏结果只标记 superseded 不物理删除，保留回滚能力 |
| mem0-inject.sh 增加注入后超 token 预算 | 低 | 低 | 每段严格控制体积，总计 < 1,500 chars |
| aiduMEM 服务重启导致短暂不可用 | 低 | 中 | user允许时重启，热加载优先 |

---

## 六、成功标准

- [ ] CoreMemory 3 个 block 正常读写，注入上下文格式正确
- [ ] Checkpoint 5 段快照正常生成和恢复
- [ ] AutoDream 蒸馏后 memory 条目数下降 ≥ 15%
- [ ] aiduMEM RSS 内存 < 200 MB（当前 151 MB）
- [ ] 全链路 `/health` 正常，所有现有 API 无退化
- [ ] 新白皮书完成

---

*设计稿，待评审后进入实现阶段。*
