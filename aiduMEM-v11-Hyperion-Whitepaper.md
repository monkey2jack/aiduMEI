# aiduMEM v11.0 "Hyperion" — AI 思想引擎技术白皮书

> **副标题**：Agent 可独立部署的专有思想系统——省 Token、极速响应、深度 5 维混合召回与 SQLite 模块化架构  
> **版本**：v11.0.0-Hyperion (亥伯龙) · 2026-07-29  
> **保密级别**：公开通用版（已完全脱敏）

---

## 一、项目概述

**aiduMEM** 是一套专为 **AI Agent** 设计的高性能、私有化**思想引擎（Thought Engine）**。它超越了传统单一的向量数据库与简单 KV 缓存，构建了一个包含**显著性分轨、动态半衰期衰减、BM25 词频加权、SQLite 工作区持久化与模块化召回引擎（RecallEngine）**的完整记忆与思维感知体系。

核心目标：**让 AI Agent 像人类一样记忆与思考——精准召回核心偏好，自动衰减废话噪声，极速响应，上下文高信噪比。**

### 1.1 核心设计哲学

```
"记忆不是单纯的堆积，而是多维度的筛选。"
"遗忘不是缺陷，而是保持思考精悍的智慧。"
"纠偏不是否决，而是认知演进的自我超越。"
```

aiduMEM 建立在 **Ebbinghaus 遗忘曲线** 的心理学模型之上，引入**Lane 显著性分轨**（Lane-aware Decay）、**知识演化追踪图谱**（Knowledge Evolution Graph）、**用户纠正信号感知**（Correction Detection）以及**五维融合召回引擎**（`RecallEngine`），使得 AI Agent 的上下文注入始终保持极高的信噪比与上下文稳定性。

---

## 二、前世今生（版本演进史）

```
v0.1 初啼基座 (mem0 向量检索 + 初始数据导入)
  ├── v1.0 无懈可击 (L0/L1/L2 分层自检 + 升级防注入测试)
  ├── v2.0 混合召回 (FTS5 中文全文索引 + 5 维加权加权融合)
  ├── v3.0 半衰期 (指数时间衰减 + Jaccard 去重 + 矛盾检测)
  ├── v4.0 Holographic (实体链接 + 多实体推理 + 12 脉扩展)
  ├── v5.0/v6.0 15 脉架构 (15 脉全链路 + 自动遗忘 + 场景聚类)
  ├── v7.0 Aion (四大自主后台线程与垃圾回收)
  ├── v8.0 Prometheus (J-space 五脉架构 + 代码瘦身 39%)
  ├── v9.0 Tahoe-Gate (相关性闸门 + 情绪分轨)
  ├── v9.3 Aletheia (Ebbinghaus 遗忘曲线 + 知识演化图谱 + 纠正信号感知)
  ├── v10.0 Clotho (三阶段大手术：归一化反逻辑修复、零字节废遗留 DB 清理)
  ├── v10.2 Mnemosyne (潮浪合并队列 + 内存/FTS 双轨检索 + API /metrics 监控)
  └── v11.0 Hyperion (亥伯龙 — 统一 RecallEngine 模块化重构 + SQLite 持久化工作区 + 真实 Salience 热度对接) ★
```

| 版本 | 代号 | 关键里程碑 |
|:---|:---|:---|
| **v1.0** | 无懈可击 | 建立 L0/L1/L2 三层防线，引入向量与元数据自检机制 |
| **v2.0** | 混合召回 | 结合 SQLite FTS5 实现中文分词搜索与初步加权 |
| **v8.0** | Prometheus | 引入 J-space 五脉架构（Ignition, Workspace, Broadcast, J-lens, Persistence） |
| **v9.3** | Aletheia | 植入 Ebbinghaus 指数遗忘曲线、Lane 分轨机制与知识演化追踪图谱 |
| **v10.2** | Mnemosyne | 异步潮浪并忆与多端点 HTTP 探针集成 |
| **v11.0** | **Hyperion (亥伯龙)** | **统一 `RecallEngine` 模块化架构 + 修复 `normalize_score` 单调截断 + 真实 Salience 热度对接 + 工作区 SQLite 持久化** |

*命名希腊神话体系*：
* **Mnemosyne** — 记忆女神（掌管"记住"）
* **Lethe** — 遗忘之河（掌管"遗忘"）
* **Aletheia** — 真理女神（Lethe 的反义词，掌管"去伪存真"）
* **Hyperion** — 亥伯龙（高空观测者、光之巨神，掌管"全景视野与统一秩序"）

---

## 三、系统架构与数据流

### 3.1 总体架构

```
┌─────────────────────────────────────────────────────┐
│                   AI Agent (Hermes)                  │
│                                                     │
│   聊天消息 → Relevance Gate (闸门) → 需要记忆？       │
│                  │ Yes          │ No                 │
│                  ▼              └→ 零注入，省 Token   │
│            RecallEngine                             │
│    ┌─────────────────────┐                          │
│    │ Stage 1: Qdrant 检索 │                          │
│    │ Stage 2: Ignition    │ ← 高置信度直达            │
│    │ Stage 3: 去重合并     │                          │
│    │ Stage 4: Ebbinghaus  │ ← 指数遗忘 + Lane 分轨   │
│    │ Stage 5: BM25/热度   │ ← 真实算分 + 显著性加权   │
│    │ Stage 6: Reranker    │ ← 最终重排序              │
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
│              │     │  workspace.db (v11)│
└──────────────┘     └────────────────────┘
```

### 3.2 J-space 五脉架构 (v11 持久化增强)

aiduMEM 内部由五条"神经脉络"驱动，灵感来自 J-space 启发式记忆理论：

| 脉络 | 模块 | 职责与 v11 升级点 |
|:---|:---|:---|
| **Ignition** | `memory_ignition.py` | 高置信度记忆直接"点火"，跳过衰减管道直达 |
| **Workspace** | `memory_workspace.py` | 活跃记忆工作区（L1 缓存），**v11 从纯内存改为 SQLite 持久化** |
| **Broadcast** | `memory_broadcast.py` | 记忆广播链：种子 → 关联 → 深度推理 |
| **J-lens** | `memory_jlens.py` | 全链路可审计 trace，每次检索可追溯留痕 |
| **Persistence** | `memory_persistence.py` | 跨会话持久化 + 分时缓存管理 |

---

## 四、v11.0 Hyperion 核心亮点与四大优势

### 4.1 统一多信号 5 维加权混合召回引擎（`RecallEngine`）

在 v11.0 中，召回算法重构为模块化的 `RecallEngine` 类，摒弃单一向量匹配的弊端，综合评估 5 大维度：

```python
score = (0.45 * vector_sim) + (0.15 * bm25_score) + (0.15 * time_decay) + (0.15 * reliability) + (0.10 * salience_score)
```

最终传入 Reranker 模型做二次重排序，解决纯语义向量搜索对精确关键词（如系统配置项、专业名词）召回不精准的痼疾。

### 4.2 Ebbinghaus 指数遗忘曲线与 Lane 分轨

采用心理学 Ebbinghaus 公式计算时间衰减：

$$\text{decay} = e^{-\lambda \times \text{multiplier} \times \text{age\_days}}$$

其中 $\lambda = 0.01$（对应约 69 天基准半衰期），衰减倍率 `multiplier` 由 Lane 分轨决定：

| Lane 分轨 | 衰减倍率 | 业务含义 |
|:---|:---|:---|
| `identity` | **0.0** | 人格身份信息（永不遗忘） |
| `preference` | **0.0** | 用户核心偏好（永不遗忘） |
| `procedural` | **0.3** | 操作步骤与工作流（慢速衰减） |
| `rule` | **0.5** | 铁律与约束规则（半速衰减） |
| `lesson` | **0.5** | 踩坑教训（半速衰减） |
| `knowledge` | **1.0** | 常规知识（标准速率） |
| `emotion` | **1.5** | 临时情绪（快速自然淡化） |

### 4.3 修正 `normalize_score` 单调截断

传统归一化在分值超过 1.0 时会发生反转（如分值越高反降为 0.5）。v11.0 引入单调截断逻辑：

```python
def normalize_score(score: float) -> float:
    """单调递增归一化，确保 score 严格限定在 [0.0, 1.0]"""
    try:
        val = float(score)
        return max(0.0, min(1.0, val))
    except (ValueError, TypeError):
        return 0.0
```

彻底杜绝因算分溢出导致低置信度记忆“逆袭”注入上下文的问题。

### 4.4 SQLite 工作区持久化与内存瘦身

活跃工作区（L1 缓存）在 v11.0 中完全重构，从原来的 Python 内存 `OrderedDict` 升级为基于 **SQLite 的 `workspace.db`**：
* 进程重启、服务部署升降级后，前序对话的 Top-20 活跃记忆无需重新初始化。
* 并发读写通过 WAL 模式与死锁重试守护，支持海量历史会话平滑切换。

---

## 五、Agent 源代码定位与完整部署指南

### 5.1 源码全景地图

项目根路径：`.`（或你克隆的源代码目录）

```
./
├── api_server.py             # HTTP REST API 主入口 (监听端口 8767)
├── ducky/                    # 核心思想引擎 Python 包
│   ├── engine.py             # 【v11 核心】RecallEngine 模块化召回引擎
│   ├── hybrid_recall.py      # 混合召回门面模块
│   ├── recall_funnel.py      # 多级召回漏斗
│   ├── memory_workspace.py   # L1 缓存与 SQLite 持久化工作区 (workspace.db)
│   ├── memory_ignition.py    # 高置信度直达点火器
│   ├── text_fts.py           # SQLite FTS5 中文全文检索与 BM25 算分
│   ├── utils.py              # 归一化与时间解析工具 (normalize_score)
│   ├── hot/health.py         # /health 探针与 /metrics 运行时指标
│   └── salience/             # 显著性分轨与热度数据库
│       ├── core.py           # Salience 记录导出接口
│       └── db.py             # salience.db 数据库操作
├── scripts/                  # 运维与恢复工具脚本
└── tests/                    # 自动化烟雾测试与单元测试套件
    └── test_smoke_api.py     # 5/5 核心功能烟雾测试
```

### 5.2 极速部署流程

```bash
# 1. 进入项目根目录
cd .

# 2. 激活 Python 虚拟环境
source venv/bin/activate

# 3. 安装依赖项
pip install -r requirements.txt

# 4. 运行全套烟雾测试套件 (验证 5/5 全部 Pass)
python3 tests/test_smoke_api.py

# 5. 启动 API 服务
python3 api_server.py
```

### 5.3 核心 API 调用契约

#### 1. GET `/health` — 健康检查与版本探针

```bash
curl -s http://127.0.0.1:8767/health
```

*响应示例*：
```json
{
  "status": "ok",
  "version": "v11.0.0-Hyperion",
  "engine": "RecallEngine",
  "workspace_db": "SQLite"
}
```

#### 2. POST `/search` — 混合召回检索

```bash
curl -s -X POST http://127.0.0.1:8767/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "系统部署与配置偏好",
    "user_id": "agent_user",
    "limit": 5
  }'
```

---

## 六、总结与展望

aiduMEM v11.0 **Hyperion（亥伯龙）** 标志着本项目进入**高内聚、低耦合、高质量上下文控制**的新阶段：为各类 AI Agent 提供标准化、零侵入、高可靠的记忆基座。

```
系统状态: ONLINE / HEALTHY
测试验证: 5 / 5 (100% Passed)
代号: Hyperion (亥伯龙)
```
