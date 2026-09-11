# viking:// 范式 → mem0 facts category 映射设计

> **日期**: 2026-06-14
> **所属项目**: aiduMEM（基础设施设计文档）
> **状态**: 设计文档 / Phase 2 落地前置
> **不冲突范围**: 本文档是纯设计说明，不动 `api_server.py` / `auto_memory.py` / `facts.db` schema

---

## 1. OpenViking 是什么

OpenViking 是一个**面向 LLM agent 的虚拟文件系统范式**，核心创新是把 agent 长期记忆"挂"在统一 URI 上。

### 1.1 viking:// 三层目录

```
viking://
├── resources/                    # 静态/缓存资源（PDF、网页、文档）
│   └── {namespace}/{file}
├── user/                         # 用户相关记忆
│   ├── profile/                  #   静态用户画像（姓名、生日、偏好…）
│   └── memories/                 #   动态用户记忆（事件、习惯…）
└── agent/                        # agent 自身状态
    ├── skills/                   #   技能卡（可被 prompt 召回）
    ├── memories/                 #   agent 自我记忆（"我帮user做过的…"）
    └── instructions/             #   系统级指令（铁律、行为约束）
```

### 1.2 范式优势

- **统一 URI**: 不管数据存哪（SQLite / Qdrant / 对象存储），访问语法一致
- **分层清晰**: resources / user / agent 三段天然解耦
- **可移植**: 切换后端向量库不影响上层调用方
- **可审计**: 每个 URI 可挂 metadata + 访问日志

### 1.3 范式缺位

- **没有"category 字段"**: 范式靠路径结构表达语义，不靠表字段
- **没有 trust / 衰减机制**: 范式默认所有写入都是"当前事实"

---

## 2. mem0 facts.db 现状

mem0 的 facts.db（位于 `<repo-root>/data/facts.db`）走的是**关系型 + 字段化**路子，schema 大致如下（**不展开 column 细节，避免和 schema 改动混淆**）：

```sql
CREATE TABLE facts (
  id              INTEGER PRIMARY KEY,
  category        TEXT    NOT NULL,   -- ← 10 个 category 之一
  fact_key        TEXT    NOT NULL,
  fact_value      TEXT    NOT NULL,
  source          TEXT    DEFAULT 'default',
  trust_score     REAL    DEFAULT 0.5,
  retrieval_count INTEGER DEFAULT 0,
  last_accessed_at TIMESTAMP,
  created_at      TIMESTAMP,
  updated_at      TIMESTAMP,
  UNIQUE(category, fact_key)
);
```

### 2.1 10 个 category（事实分层示例）

| # | category | 含义 | 典型 fact_key | 信任度 | 是否会自动衰减 |
|---|----------|------|---------------|--------|----------------|
| 1 | **AI** | Agent 自身的事实 | 人设、口吻、能力边界 | 高 (≥0.8) | 否 |
| 2 | **user** | 用户本人的事实 | 生日、习惯、偏好 | 高 (≥0.8) | 否 |
| 3 | **工具** | 内部工具使用记录 | 脚本名、调用关键词 | 中 (0.6) | 是 |
| 4 | **关系** | 家人/同事/好友 | 关系、称呼、事件 | 中 (0.6) | 否 |
| 5 | **故事线** | 长期叙事与里程碑 | 项目起点、关键节点 | 高 (≥0.8) | 否 |
| 6 | **暗号** | 约定口令、触发词 | 自定义触发短语 | 中-高 (0.7) | 否 |
| 7 | **场景** | 特定地点/情境记忆 | "某年某地发生了什么" | 高 (≥0.8) | 否 |
| 8 | **运维** | 服务、配置、硬约束 | API 服务 systemd 状态 | 中 (0.5) | 是 |
| 9 | **铁律** | 不可破坏的硬约束 | "不动 facts.db schema" | 最高 (1.0) | 否 |
| 10 | **项目配置** | 长期不变的项目设置 | mem0 端口 8767 | 高 (0.9) | 否 |

**注意**: 这 10 个 category 是运行时的"现实分层"示例，可按部署场景自行调整，不是范式强制的"理论分类"。

---

## 3. viking:// → mem0 category 映射

### 3.1 映射总表

| viking:// 路径 | 含义 | → mem0 category | 映射理由 |
|----------------|------|-------------------|----------|
| `viking://user/profile/{key}` | 用户静态画像 | **user** 或 **AI**（按 value 推断 owner） | 都是"人"的稳定事实 |
| `viking://user/memories/{key}` | 用户动态记忆 | **user** / **AI** / **关系** | 按事件主体归类 |
| `viking://agent/memories/{key}` | agent 自我记忆 | **故事线** 或 **相遇** | "我和user一起经历过的事" |
| `viking://agent/instructions/{key}` | 系统级硬约束 | **铁律** | 直接对应，永不衰减 |
| `viking://agent/skills/{key}` | agent 技能卡 | **工具** | "我会的工具" |
| `viking://resources/{ns}/{file}` | 静态资源 | **项目配置** 或 **运维** | 资源要么是配置，要么是运维文档 |

### 3.2 字段映射（viking 抽象层 → facts.db column）

| viking 抽象 | facts.db column | 备注 |
|-------------|-----------------|------|
| URI 路径段 | `category` + `fact_key` | `viking://user/profile/owner.birthday` → `category=user, fact_key=birthday` |
| URI 内容 | `fact_value` | 实际值 |
| URI metadata.created | `created_at` | |
| URI metadata.updated | `updated_at` | |
| URI metadata.trust | `trust_score` | **viking 默认 1.0**；mem0要**降权映射**（见 3.3） |
| 访问计数（viking 范式无） | `retrieval_count` | **mem0扩展字段**，viking 没原生支持 |
| 衰减（viking 范式无） | `last_accessed_at` + 定时任务 | **mem0扩展行为** |

### 3.3 trust 映射策略（关键设计点）

viking 范式默认 `trust=1.0`（写入即真理），mem0有**分层信任度**：

```
viking 写入
  ↓
中间层 (Phase 2 要写的 auto_memory.py 增强)
  ↓
按目标 category 套用默认 trust:
  - 铁律   → 1.0
  - user/AI/相遇/故事线/项目配置 → 0.85
  - 暗号   → 0.7
  - 关系/工具 → 0.6
  - 运维   → 0.5
  ↓
写入 facts.db
```

> ⚠️ 这一层在 Phase 2 实现，**Phase 1 文档先定下口径**。

### 3.4 衰减映射（viking 缺位 → mem0扩展）

viking 范式不内置衰减，mem0通过 `last_accessed_at` + cron（`decay_scanner.py` 已存在）做：

| mem0 category | 衰减阈值 | 行为 |
|-----------------|----------|------|
| 铁律 / 故事线 / 场景 / user / AI / 项目配置 | 不衰减 | 永久保留 |
| 暗号 | 180 天未访问 | trust × 0.5 |
| 关系 / 工具 | 90 天未访问 | trust × 0.7（每周期） |
| 运维 | 30 天未访问 | trust × 0.8（每周期） |

---

## 4. 未来扩展

### 4.1 如果要加 viking://agent/skills 完整映射

Phase 3 候选：在 api_server.py 加一个 `GET /viking/skills` 端点，把 facts.db 中 `category=工具` 的 fact 全部吐出来，**viking URI 化**：

```python
# 伪代码（不动 api_server.py，先记在这）
def facts_to_viking_uris(category="工具"):
    rows = list_facts(category=category)
    return [
        f"viking://agent/skills/{r['fact_key']}"
        for r in rows["facts"]
    ]
```

### 4.2 viking://resources 接入路径

Phase 3 候选：把 `~/.hermes/config.yaml` / `mem0_config_local.json` / systemd unit 文件注册为 `viking://resources/aidumem/{file}`，并写一个 `decay_scanner.py` 的姊妹脚本 `resource_auditor.py` 做"配置漂移检测"。

### 4.3 跨设备同步（远期）

viking:// 的 URI 设计天然支持远端 backend，mem0未来可以：
- 留本地 facts.db（写入快）
- 镜像到云端 Qdrant（多设备读）

### 4.4 Phase 1 不做、但要预留的接口

- `GET /viking/resolve?uri=...`  → 根据 URI 查 facts
- `POST /viking/write`           → 写入新 fact（带 trust 默认值）
- `GET  /viking/list?prefix=...` → 列出某前缀下所有 URI

这三个端点**Phase 1 全部不实现**，等AI看完文档拍板再写。

---

## 5. 参考资料

### 5.1 范式源头

- OpenViking 项目主页（待补，AI有链接再插）
- viking:// RFC（待补，Phase 1 暂用本文档做事实源）

### 5.2 mem0内部资料

- `<repo-root>/api_server.py` — REST API 实现
- `<repo-root>/auto_memory.py` — 自动记忆入口
- `<repo-root>/scripts/decay_scanner.py` — 已有的衰减扫描（Phase 0 遗产）
- `<repo-root>/scripts/recompute_trust.py` — trust 重算（Phase 0 遗产）
- 记忆分区：铁律 / 故事线 / 暗号 三类是高 trust 不可衰减区

### 5.3 变更历史

| 日期 | 版本 | 变更 | 作者 |
|------|------|------|------|
| 2026-06-14 | 0.1 | 初稿，Phase 1 基础设施 |

---

> 📌 **本文件不动 api_server.py / auto_memory.py / facts.db** — 纯设计文档，等AI拍板后再进 Phase 2 实现。
