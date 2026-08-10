# aiduMEM v9.3 "Aletheia" — AI 思想引擎技术白皮书

> **副标题**：Agent 可部署的专有思想引擎——省 Token、快响应、稳上下文
>
> 版本：v9.3.0-Aletheia · 2026-07-27

---

## 一、项目概述

**aiduMEM** 是一套面向 AI Agent 的**私有化思想引擎**（Thought Engine）。它不是简单的向量数据库，也不是单纯的缓存——它是一个**具备遗忘能力、知识演化意识、多轨衰减策略和自愈纠偏能力**的完整记忆系统。

核心目标只有一个：**让 AI Agent 真正像人一样记忆——记该记的，忘该忘的，纠该纠的。**

### 设计哲学

```
"记忆不是堆积，而是筛选。"
"遗忘不是缺陷，而是智慧。"
```

aiduMEM 建立在 **Ebbinghaus 遗忘曲线**的心理学模型之上，引入了**分轨衰减**（Lane-aware Decay）、**知识演化追踪**（Knowledge Evolution Graph）和**用户纠正信号感知**（Correction Detection），使得 AI Agent 的上下文注入始终保持**高信噪比**。

---

## 二、版本演进

```
v0 初啼 (Day 1)
  → v1 三层自检 (L0/L1/L2)
    → v2 FTS5 + 混合召回
      → v3 半衰期 + 去重
        → v4 实体链接 + 多实体推理
          → v5/v6 15 脉 + 自动遗忘
            → v7 四大自主模块
              → v8 J-space 五脉架构 (瘦身 39%)
                → v9 相关性闸门 + 情绪衰减
                  → v9.1 异步潮浪并忆
                    → v9.2 Lethe: 初步引入遗忘曲线
                      → v9.3 Aletheia: 四大功能完全植入
```

| 版本 | 代号 | 关键里程碑 |
|------|------|-----------|
| v0 | 初啼 | mem0 向量基座 + 初始数据导入 |
| v1 | 无懈可击 | L0/L1/L2 分层自检 + 升级免疫测试体系 |
| v2 | 混合召回 | FTS5 中文全文索引 + 5 维加权融合 |
| v3 | 半衰期 | 时间衰减 + 去重 + 矛盾检测 v1 |
| v4 | Holographic | 实体链接 + 多实体推理 + 12 脉扩展 |
| v5/v6 | 15 脉 | 15 脉全链路 + 自动遗忘 + 场景聚类 |
| v7 | Aion | 4 大自主后台模块 |
| v8 | Prometheus | J-space 五脉架构 + 代码瘦身 39% ★ |
| v9 | Tahoe-Gate | 相关性闸门（查询前过滤） + 情绪衰减 |
| v9.1 | Mnemosyne | 潮浪并忆（会话级合并） + 全链路异步 |
| v9.2 | Lethe | 初步引入 Ebbinghaus 遗忘曲线 |
| **v9.3** | **Aletheia** | **四大功能完全植入 + 命名对齐** |

命名体系延续希腊神话：
- **Mnemosyne** — 记忆女神（掌管"记住"）
- **Lethe** — 遗忘之河（掌管"遗忘"）
- **Aletheia** — 真理女神（Lethe 的反义词，掌管"去伪存真"）

---

## 三、系统架构

### 3.1 总体架构

```
┌─────────────────────────────────────────────────────┐
│                   AI Agent (Hermes)                  │
│                                                     │
│   聊天消息 → Relevance Gate (闸门) → 需要记忆？       │
│                  │ Yes          │ No                 │
│                  ▼              └→ 零注入，省 Token   │
│          Recall Funnel                              │
│    ┌─────────────────────┐                          │
│    │ Stage 1: Qdrant 检索 │                          │
│    │ Stage 2: Ignition    │ ← 高置信度直达            │
│    │ Stage 3: 去重合并     │                          │
│    │ Stage 4: Ebbinghaus  │ ← 指数遗忘 + Lane 分轨   │
│    │ Stage 5: 物理过滤     │ ← superseded 状态排除    │
│    │ Stage 6: Top-K 截断  │                          │
│    └─────────────────────┘                          │
│              ▼                                      │
│      上下文注入 (Context Injection)                   │
└─────────────────────────────────────────────────────┘
        │                       │
        ▼                       ▼
┌──────────────┐     ┌────────────────────┐
│   Qdrant     │     │   SQLite 数据库集群  │
│ (向量存储)    │     │                    │
│ 向量嵌入  │     │  facts.db          │
│ 1024 维向量   │     │  salience.db       │
│              │     │  text_fts.db       │
│              │     │  observations.db   │
│              │     │  scenes.db         │
└──────────────┘     └────────────────────┘
```

### 3.2 J-space 五脉架构

aiduMEM 内部由五条"神经脉络"驱动，灵感来自 J-space 启发式记忆理论：

| 脉络 | 模块 | 职责 |
|------|------|------|
| **Ignition** | `memory_ignition.py` | 高置信度记忆直接"点火"，跳过衰减管道 |
| **Workspace** | `memory_workspace.py` | 活跃记忆工作区（L1 缓存），最近 20 条热记忆 |
| **Broadcast** | `memory_broadcast.py` | 记忆广播链：种子→关联→深度推理 |
| **J-lens** | `memory_jlens.py` | 全链路可审计 trace，每次检索留痕 |
| **Persistence** | `memory_persistence.py` | 跨会话持久化 + 分时缓存 |

### 3.3 写入链路

```
用户消息 → Correction Detection (纠正信号检测)
              │
              ▼
         Layer 1 自检
         ├─ 去重（Jaccard + 语义双重）
         ├─ 矛盾检测
         ├─ 容量检查
         └─ Knowledge Evolution (知识演化追踪)
              │
              ▼
         Salience 分轨 → Lane 分配（identity/preference/emotion/...）
              │
              ▼
         Speed Pipeline (异步潮浪并忆)
         ├─ 短文本快路径
         ├─ 会话合并队列
         └─ LLM 抽取 → Qdrant 写入 + FTS5 索引
```

---

## 四、v9.3 Aletheia 四大新特性

### 4.1 Ebbinghaus 指数遗忘曲线

**问题**：传统线性衰减不符合人类记忆规律。一条 30 天前的记忆和一条 60 天前的记忆，在线性模型中差距太小。

**方案**：引入心理学中的 Ebbinghaus 遗忘公式：

```
decay = e^(-λ × multiplier × age_days)
```

其中：
- `λ = 0.01`（对应约 69 天半衰期）
- `multiplier` 由 Lane 分轨决定（见下表）

| Lane | 衰减倍率 | 含义 |
|------|---------|------|
| identity | 0.0 | 身份信息永不遗忘 |
| preference | 0.0 | 偏好永不遗忘 |
| procedural | 0.3 | 操作步骤慢衰减 |
| rule | 0.5 | 规则半速衰减 |
| lesson | 0.5 | 踩坑教训半速衰减 |
| evidence | 0.7 | 证据正常偏慢 |
| knowledge | 1.0 | 知识正常衰减 |
| emotion | 1.5 | 情绪快速衰减 |
| general | 1.0 | 通用正常衰减 |

**效果**：身份和偏好类记忆（如"用户是工程师"）永远保鲜，而情绪类记忆（如"今天心情不好"）以 1.5 倍速率自然淡化。

### 4.2 知识演化追踪

**问题**：用户的知识会更新。"喜欢喝普洱茶"在三个月后可能变成"改为喝龙井茶"。旧记忆如果不被标记为"已取代"，就会污染上下文。

**方案**：在写入新记忆时，自动检测与已有记忆的演化关系：

```
写入新记忆 E
  → 检索 top-5 相似旧记忆
    → 对每条旧记忆计算：
        1. Jaccard 字符相似度（阈值 0.12）
        2. 中文共同话题检测（2+ 汉字词交集）
        3. 极性翻转检测（"不再"、"改为"、"取代"等关键词）
    → 判定关系类型：
        - replaces: 新记忆取代旧记忆（旧记忆标记为 superseded）
        - enriches: 新记忆丰富旧记忆（保持共存）
        - confirms: 新记忆确认旧记忆（增强置信度）
        - challenges: 新记忆挑战旧记忆（标记待验证）
```

**数据库表结构**：

```sql
-- 记忆状态机
CREATE TABLE memory_states (
    memory_id TEXT PRIMARY KEY,
    state     TEXT DEFAULT 'active',    -- active | superseded | archived
    reason    TEXT,
    source    TEXT,
    updated_at TEXT
);

-- 知识演化关系图
CREATE TABLE knowledge_evolution (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT,       -- 新记忆 ID
    target_id   TEXT,       -- 旧记忆 ID
    relation    TEXT,       -- replaces | enriches | confirms | challenges
    confidence  REAL,
    reason      TEXT,
    created_at  TEXT
);
```

**效果**：被取代的旧记忆在 Recall Funnel 的 Stage 5 中被**物理过滤**，永远不会再注入上下文。

### 4.3 用户纠正信号感知

**问题**：用户说"不对，你记错了"时，Agent 应该立刻进入深度记忆检索模式，而不是在闸门处被判为"不需要记忆"。

**方案**：在 Relevance Gate 中增加纠正信号正则检测：

```python
CORRECTION_PATTERNS = re.compile(
    r'不对|不是这|你记错|错了|no, |wrong|actually|not really|记错了|你说错',
    re.IGNORECASE
)
```

当检测到纠正信号时，**强制返回 `needs_memory=True`**，绕过闸门的常规判断，确保 Agent 立刻自我纠偏。

### 4.4 Memory Health Report

**端点**：`GET /api/memory/health`

**返回示例**：

```json
{
  "status": "ok",
  "version": "9.3.0-Aletheia",
  "report": {
    "lane_distribution": {
      "general": 229,
      "knowledge": 4,
      "preference": 3,
      "procedural": 8,
      "rule": 4
    },
    "state_distribution": {
      "active": 245,
      "superseded": 3
    },
    "recent_7d_growth": 195,
    "evolution_relationships": {
      "enriches": 7,
      "replaces": 3
    }
  }
}
```

一目了然地掌握记忆系统的健康状况、分轨分布和演化趋势。

---

## 五、核心模块清单

### 5.1 ducky 智能模块包

```
ducky/
├── layer1_selfcheck.py      # L1 写入自检 + 知识演化
├── recall_funnel.py          # 六阶检索漏斗
├── memory_gate.py            # 相关性闸门 + 纠正感知
├── memory_ignition.py        # 高置信度点火
├── memory_workspace.py       # L1 活跃记忆缓存
├── memory_broadcast.py       # 记忆广播链
├── memory_jlens.py           # 可审计 trace
├── memory_persistence.py     # 跨会话持久化
├── hybrid_recall.py          # 5 维加权混合召回
├── text_fts.py               # FTS5 中文全文索引
├── tool_envelope.py          # 统一返回契约
├── instinct_graduation.py    # Instinct→Skill 自动毕业
├── mem0_runtime.py           # mem0 运行时管理
├── utils.py                  # 工具函数 + 连接工厂
├── salience/                 # 显著性子系统
│   ├── config.py             # Lane 衰减常量
│   ├── core.py               # Lane 分轨检测
│   ├── db.py                 # salience.db 操作
│   ├── conflict.py           # 矛盾检测
│   ├── metrics.py            # 显著性统计
│   ├── audit.py              # 审计日志
│   └── lesson_verify.py      # 教训验证
├── speed/                    # 异步高速写入子系统
│   ├── pipeline.py           # 写入主流程
│   ├── coalesce.py           # 会话合并队列
│   ├── cache.py              # 抽取结果缓存
│   ├── fastpath.py           # 短文本快路径
│   ├── jobs.py               # 异步 job 状态
│   ├── config.py             # 配置
│   ├── patch.py              # LLM 请求补丁
│   └── stats.py              # 命中统计
├── hot/                      # 热路径 API
│   ├── search.py             # 检索
│   ├── add.py                # 写入
│   ├── crud.py               # CRUD
│   └── health.py             # 健康检查
└── extended/                 # 扩展功能
    ├── auto_memory.py        # 自动记忆抽取
    └── routes.py             # 扩展路由
```

### 5.2 技术参数

| 参数 | 值 |
|------|---|
| 代码规模 | ~9,800 行 Python，59 个模块 |
| 向量引擎 | Qdrant（嵌入式模式，无需独立服务） |
| 嵌入模型 | OpenAI text-embedding-3-small |
| 向量维度 | 1024 维 |
| 全文索引 | SQLite FTS5（中文分词） |
| 数据库 | SQLite × 7（facts / salience / text_fts / observations / scenes / text_index / text） |
| API 框架 | FastAPI (Uvicorn) |
| 运行端口 | 8767 |

---

## 六、Recall Funnel 六阶检索漏斗

这是 aiduMEM 的核心读路径。每次 Agent 需要记忆上下文时，查询经过六个阶段的精炼：

```
Stage 1: Qdrant 向量检索
  → 基于 向量嵌入的语义相似度检索，返回 top-N 候选
  
Stage 2: Ignition 点火
  → 相似度 > 0.85 的候选直接"点火"进入结果池，跳过后续管道

Stage 3: 去重合并
  → Jaccard 字符级 + 语义级双重去重，合并高度相似的条目

Stage 4: Ebbinghaus 指数遗忘
  → 按 Lane 分轨应用差异化衰减：
    身份/偏好 → 永不衰减
    知识/通用 → 正常衰减（~69天半衰期）
    情绪       → 1.5x 加速衰减

Stage 5: 物理过滤
  → 状态为 superseded（已被取代）的记忆直接剔除
  → 不占用任何上下文配额

Stage 6: Top-K 截断
  → 按最终分数排序，截取 top-K 注入上下文
```

每次检索都会生成 **J-lens trace**（完整审计记录），记录每个阶段的候选数量、耗时和过滤原因。

---

## 七、Relevance Gate 相关性闸门

这是 aiduMEM 的"省钱利器"。不是所有查询都需要记忆——"今天天气怎么样"这种问题去检索记忆库是纯浪费。

```
查询 → Relevance Gate
        │
        ├─ 纠正信号检测（"不对/记错了/wrong"）→ 强制通过
        │
        ├─ 关键词匹配（记忆相关词汇）→ 通过
        │
        ├─ 缓存命中（15秒 TTL）→ 返回缓存结果
        │
        └─ LLM 轻量判断 → 通过 / 拒绝
```

实测效果：闸门过滤掉约 **40-60%** 的无关查询，直接节省对应的 Qdrant 检索和 embedding 调用开销。

---

## 八、Speed Pipeline 异步潮浪并忆

v9.1 引入的"潮浪并忆"（Mnemosyne）机制，核心思想：**会话中的多条消息不要逐条写入，而是合并后一次性抽取写入**。

```
消息流:  M1 → M2 → M3 → M4 → M5
              │
              ▼
         合并队列 (Coalesce Queue)
         ├─ 短文本快路径: 纯文本 < 50字 → 直接写入
         ├─ 会话窗口: 同一会话 60秒内的消息合并
         └─ LLM 批量抽取: 合并后的文本一次性提取事实
              │
              ▼
         异步 Job 队列 → 后台写入 Qdrant + SQLite
```

**收益**：
- LLM 抽取调用减少 **60-80%**
- embedding 调用减少 **40-60%**
- 写入延迟对用户完全透明（异步接单，聊天体感零阻塞）

---

## 九、Lane 分轨感知系统

每条记忆被自动分配到一个"轨道"（Lane），不同轨道拥有不同的衰减速率和保护等级。

### 9.1 自动分轨

基于关键词匹配的轻量分轨检测：

| Lane | 触发关键词示例 | 保护等级 |
|------|--------------|---------|
| identity | 我是、我叫、我住在 | ★★★★★ 永不衰减 |
| preference | 喜欢、偏好、最爱 | ★★★★★ 永不衰减 |
| procedural | 步骤、配置、执行 | ★★★★ 慢衰减 |
| rule | 必须、禁止、铁律 | ★★★★ 半速衰减 |
| lesson | 踩坑、报错、修复 | ★★★★ 半速衰减 |
| evidence | 发现、测试、验证 | ★★★ 正常偏慢 |
| knowledge | API、版本、路径 | ★★ 正常衰减 |
| emotion | 开心、难过、想 | ★ 快速衰减 |

### 9.2 分轨衰减公式

```
最终分数 = 原始相似度 × e^(-0.01 × lane_multiplier × age_days)
```

- identity/preference: `multiplier = 0.0` → 衰减因子永远为 1.0
- emotion: `multiplier = 1.5` → 30天后衰减到 ~64%，90天后衰减到 ~26%
- knowledge: `multiplier = 1.0` → 30天后衰减到 ~74%，90天后衰减到 ~41%

---

## 十、资源消耗

### 10.1 硬件需求

| 资源 | 最低配置 | 推荐配置 |
|------|---------|---------|
| CPU | 1 核 | 2 核 |
| 内存 | 512MB | 1GB |
| 磁盘 | 200MB（代码+依赖） | 500MB（含数据增长） |
| GPU | 不需要 | 不需要 |

### 10.2 实际运行数据

以下为生产环境（2 核 / 3.4GB RAM）下 248 条记忆的实测数据：

| 指标 | 数值 |
|------|------|
| 服务常驻内存 | ~670MB（含 Qdrant 嵌入式 + Python 运行时） |
| 峰值内存 | ~720MB |
| 数据库磁盘 | ~73MB（7 个 SQLite 库 + Qdrant 存储） |
| 代码 + 依赖 | ~210MB（含 venv） |
| 启动时间 | < 3 秒 |
| 单次检索延迟 | 50-200ms（含向量检索 + FTS5 + 六阶漏斗） |
| 单次写入延迟 | < 10ms（异步接单），后台 LLM 抽取 1-3 秒 |

### 10.3 外部 API 消耗

| 服务 | 用途 | 调用频率 |
|------|------|---------|
| OpenAI Embedding | 向量嵌入 | 每次写入/检索 1 次 |
| OpenAI / 兼容 LLM | 事实抽取 + 闸门判断 | 每次写入 1 次（合并后） |

闸门和潮浪合并机制可节省 **50-70%** 的外部 API 调用。

---

## 十一、借鉴融合

aiduMEM 站在众多优秀开源项目的肩膀上，取各家之长：

| 来源项目 | 吸收了什么 |
|---------|-----------|
| **mem0** | 向量存储基座（Qdrant + 向量嵌入） |
| **memory-os** | 7 层架构 · Facts 表 · Bayesian trust · 4 级权威 · FTS5 |
| **OpenViking** | L0/L1/L2 分层自检 · 目录递归 |
| **Aion Memory** | Layer 1 自检 · Recall Funnel · Instinct→Skill 蒸馏 |
| **Hindsight TEMPR** | 5 维混合召回 · 时效权重 |
| **DIKW** | 数据→信息→知识→智慧 金字塔 |
| **J-space** | 五脉架构（Ignition / Workspace / Broadcast / J-lens / Persistence） |
| **Osaurus Memory** | 相关性闸门 · 延迟蒸馏 · 显著性衰减 |
| **EchoMind** | Ebbinghaus 遗忘曲线 · 知识演化图谱 · 用户纠错信号感知 |
| **Honcho** | Peer 记忆 · 跨用户关系 |
| **RetainDB** | 偏好存储 · Delta 增量 |

---

## 十二、部署指南

### 12.1 环境要求

- Python 3.10+
- Linux（推荐 Ubuntu 22.04+）
- 无需 GPU
- 无需独立 Qdrant 服务（使用嵌入式模式）

### 12.2 快速启动

```bash
# 1. 克隆仓库
git clone <repo_url> && cd aiduMEM

# 2. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
export OPENAI_API_KEY="your-api-key"
# 或使用兼容 OpenAI 协议的其他 LLM 服务

# 5. 启动服务
python3 api_server.py
# 默认监听 http://0.0.0.0:8767
```

### 12.3 健康检查

```bash
# 基本健康
curl http://localhost:8767/health

# 记忆健康报告
curl http://localhost:8767/api/memory/health
```

### 12.4 核心 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/add` | 写入记忆（支持异步模式） |
| POST | `/search` | 检索记忆（六阶漏斗） |
| GET | `/health` | 服务健康检查 |
| GET | `/api/memory/health` | 记忆健康报告 |
| POST | `/gate` | 相关性闸门判断 |
| GET | `/api/stats` | 用量统计 |

---

## 十三、与同类项目对比

| 特性 | aiduMEM | mem0 (原版) | EchoMind | Zep | MemoryOS |
|------|---------|------------|----------|-----|----------|
| 向量检索 | ✅ Qdrant | ✅ Qdrant | ❌ SQLite | ✅ | ✅ |
| 全文索引 | ✅ FTS5 | ❌ | ❌ | ✅ | ❌ |
| 遗忘曲线 | ✅ Ebbinghaus 指数 | ❌ | ✅ 线性 | ❌ | ❌ |
| 分轨衰减 | ✅ 9 轨道 | ❌ | ❌ | ❌ | 部分 |
| 知识演化 | ✅ 4 种关系 | ❌ | ❌ | ❌ | ❌ |
| 纠正感知 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 相关性闸门 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 异步写入 | ✅ 潮浪并忆 | ❌ | ❌ | ✅ | ❌ |
| 可审计 trace | ✅ J-lens | ❌ | ❌ | ❌ | ❌ |
| 记忆健康报告 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 中文优化 | ✅ 原生 | ❌ | ❌ | ❌ | ❌ |
| 无 GPU 部署 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 常驻内存 | ~670MB | ~200MB | ~100MB | ~500MB | ~300MB |

---

## 十四、设计亮点总结

1. **Ebbinghaus 遗忘曲线 + 分轨衰减**：身份永记，情绪速忘，知识自然淡化——这是市面上唯一实现心理学级分轨遗忘的 Agent 记忆系统。

2. **知识演化追踪**：旧知识不删除、不堆积，而是优雅地被新知识"取代"，形成可追溯的演化链路。

3. **用户纠正信号感知**：Agent 能感知到用户在纠错，瞬间切换到自我纠偏模式。

4. **Relevance Gate 省钱利器**：40-60% 的无关查询在闸门处就被拦截，直接省掉向量检索和 embedding 开销。

5. **潮浪并忆**：会话级消息合并后再抽取，LLM 调用减少 60-80%。

6. **J-lens 全链路审计**：每次检索的每个阶段都有 trace，出问题能秒级定位。

7. **物理级隔离**：被取代的记忆不是"降权"——是**物理过滤**，绝不出现在上下文中。

8. **中文原生优化**：FTS5 中文分词 + 中文共同话题检测 + 中文纠正信号正则，专为中文 Agent 场景打造。

---

> **aiduMEM v9.3 "Aletheia"** — 让 AI 不只是记住，更是懂得去伪存真。
>
> 真理女神阿勒忒亚，是遗忘之河 Lethe 的反义词。
> 她不让你遗忘真理，也不让虚假的记忆污染你的思想。
