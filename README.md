# 🤔 aiduMEM — AI 思想引擎

> **优忆思理念——有上下文优化，有记忆体系，有思考集成**
>
> **不只是记忆 — 是思考。**

```
优化不是改重构代码，而是植入优秀的逻辑；
记忆不是记事，而是不忘过往的点点滴滴；
思考不是 reasoning，而是做所有事都有 reason，有 result。
```

[![Version](https://img.shields.io/badge/version-14.0.1%20·%20Aegis·埃癸斯-blue.svg)](https://github.com/monkey2jack/aiduMEM)
[![PyPI](https://img.shields.io/pypi/v/aidumem.svg)](https://pypi.org/project/aidumem/)
[![Docker Image](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/monkey2jack/aiduMEM/pkgs/container/aidumem)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-yellow.svg)](https://www.python.org/)
[![Built on mem0](https://img.shields.io/badge/built%20on-mem0-orange.svg)](https://github.com/mem0ai/mem0)

**中文** | **[📖 English](README_EN.md)**

---

## aiduMEM 是什么？

aiduMEM（优忆思）是一个 **AI 思想引擎** —— 为 AI Agent 提供持久化记忆与推理能力。它不是简单的键值存储，而是让 AI **会记忆、会思考、会进化**。

基于 [mem0](https://github.com/mem0ai/mem0) 构建，aiduMEM 在其之上搭建了一套完整的 **认知架构**：

| 层级 | 做什么 | 核心特性 |
|------|--------|----------|
| 🧠 **记忆** | 在对的时间找到对的回忆 | Ebbinghaus 遗忘曲线 + BM25/trigram + 向量混合检索 |
| 🔍 **闸门** | 只检索真正相关的内容 | 相关性闸门拦截无关上下文 —— 快 100 倍 |
| 🌊 **潮浪** | 批量 LLM 提取，不逐条调用 | 合并队列：异步短消息 → 单次 LLM 调用 |
| 📊 **进化** | 知识生长与自我纠错 | 信任评分、用户纠正感知、知识整合 |
| ⏳ **遗忘** | 遗忘是特性，不是 bug | 情绪轨道加速衰减、身份轨道零衰减 |
| 🕰️ **克罗诺斯** | 时间感知的有效期 | 双时间轴（valid_from / valid_to），过期事实降权 |
| 🏛️ **万神殿** | 多 Agent 共享一套记忆 | 联邦身份 + MoE 门控 + 四级降级检索 |
| 🛡️ **埃癸斯** | 零硬编码，换机即跑 | 身份/路径/词表全部环境变量注入 |

---

## 架构

```
┌──────────────────────────────────────────────────┐
│              🤔 aiduMEM — AI 思想引擎             │
│              FastAPI REST API :8767               │
├──────────────────────────────────────────────────┤
│  hot/        → 搜索、添加、增删改查（主路径）       │
│  speed/      → 异步合并 + 快速通道                 │
│  pipeline/   → 召回漏斗 + 混合检索                 │
│  salience/   → 信任评分 + 冲突检测                 │
│  legacy/     → 事实、观察、场景                    │
│  extended/   → 自动记忆、工作区、广播               │
│  federation/ → 多 Agent 联邦 · MoE 门控             │
├──────────────────────────────────────────────────┤
│  mem0（向量记忆）+ Qdrant（向量存储）              │
│  facts.db（结构化知识）                            │
│  FTS5 trigram（全文搜索兜底）                      │
└──────────────────────────────────────────────────┘
```

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/monkey2jack/aiduMEM.git
cd aiduMEM

# 2. 创建虚拟环境
python3.12 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置（复制并编辑）
cp mem0_config_local.json.example mem0_config_local.json
# 编辑 mem0_config_local.json，填入你的 LLM 和 Embedding API Key

# 5. 启动
python api_server.py
# API 运行在 http://localhost:8767
```

## 接口列表

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/search` | 搜索记忆（混合：向量 + BM25 + 相关性闸门） |
| `POST` | `/add` | 添加记忆（默认异步合并） |
| `DELETE` | `/delete` | 按 ID 删除记忆 |
| `GET` | `/health` | 健康检查 + 探针诊断 |
| `POST` | `/search_trace` | 带完整执行链路的搜索 |
| `POST` | `/graduate` | 扫描并毕业低价值记忆 |
| `POST` | `/scene/cluster` | 将观察聚类为场景 |
| `GET` | `/usage` | Token 用量统计 |
| `GET` | `/add/coalesce/stats` | 潮浪合并统计 |
| `GET` | `/federation/recall` | 联邦检索（MoE 门控自动决策热/联邦通道） |
| `POST` | `/federation/facts/add` | 联邦写入（自动去重 + 分层 + 归属） |
| `GET` | `/federation/agents` | Agent 列表（含事实数与在线状态） |
| `POST` | `/federation/agents/register` | 注册 Agent 到联邦 |
| `GET` | `/federation/broadcast` | 拉取其他 Agent 的新共享事实 |
| `GET` | `/federation/awareness` | 联邦态势摘要 |
| `GET` | `/federation/tiers` | 分层分布与衰减配置 |

### 示例：搜索

```bash
curl -s -X POST http://localhost:8767/search \
  -H "Content-Type: application/json" \
  -d '{"query": "我之前说过项目截止日期是什么？", "user_id": "me", "limit": 5}'
```

### 示例：添加记忆

```bash
curl -s -X POST http://localhost:8767/add \
  -H "Content-Type: application/json" \
  -d '{"messages": "[{\"role\":\"user\",\"content\":\"项目截止日期是3月15号\"}]", "user_id": "me"}'
```

## aduMEM 的独特之处

### 🔮 相关性闸门（Relevance Gate）
普通 RAG 系统对每条消息都去搜索记忆。aiduMEM 的**相关性闸门**用启发式 + 动态实体匹配判断当前消息是否真的需要记忆检索。日常闲聊直接跳过 → **Token 消耗降低 100 倍**，响应速度从 10ms 降到 1ms。

### 🌊 潮浪并忆（Tidal Coalescing）
短消息不逐条调用 LLM。异步缓冲后按 session 分组，一次 LLM 调用处理多条。Tech/intimate/default 三档策略，快冲慢攒各取所需。

### ⏳ 遗忘曲线衰减（Ebbinghaus Decay）
记忆有保质期。Identity 和 Preference 是永久轨道（零衰减），Emotion 是加速衰减（1.5 倍），一般事实按标准遗忘曲线自然消退。**让 AI 学会忘记不重要的事。**

### 🕰️ 克罗诺斯双时间轴（Chronos Dual Timeline）
`valid_from` / `valid_to` 时间窗口：过期事实降权但不删除，未生效事实排在后面。所有铁律类记忆（identity/preference lane）永不过期。

### 🏛️ 万神殿联邦记忆（Pantheon Federation）
> 万神殿里住着所有神，但每次只请出需要的那一位。

借鉴 MoE（Mixture-of-Experts）思想：**底层建成完整的多 Agent 联邦基础设施，日常只激活当前 Agent 的热通道**。

- **联邦身份**：每条记忆都带 `agent_id` / `profile` / `shared`，多个 Agent 可共用一套库而互不污染
- **MoE 门控**：默认走热通道（一次 SQL，5ms 级）；仅在显式请求或查询含联邦意图时才唤起其他 Agent。单 Agent 环境永远不付联邦成本
- **四级无缝降级**：L1 本 Agent → L2 分层加权 → L3 同 profile 联邦 → L4 跨 profile 全局。任何一级异常自动跳下一级，永不整链失败
- **分层衰减**：`episodic` 事件 30 天、`semantic` 配置 180 天、`procedural` 铁律**永不衰减**。衰减只降权不删行
- **写入去重**：Jaccard 三态判定——≥0.85 合并、≥0.70 更新、<0.70 新增。不写垃圾比事后清理便宜一百倍
- **游标广播**：拉取其他 Agent 的新共享事实，不重不漏、只读聚合不产生副本

```bash
# 注册一个 Agent 到联邦
curl -X POST "http://localhost:8767/federation/agents/register?agent_id=agent_b&profile=default"

# 联邦检索（门控自动决策）
curl "http://localhost:8767/federation/recall?query=项目截止日期&agent_id=agent_b&top_k=5"
```

### 🛡️ 埃癸斯护盾（Aegis · v14 新增）
> 神盾护住的不是代码，是代码背后的人。

仓库里没有任何硬编码的身份、绝对路径、服务器地址或密钥。仓库根由 `__file__` 自动解析，一切可变项走环境变量注入（见 [环境变量](#环境变量)）。克隆到任何目录、任何机器，`python api_server.py` 直接跑。

### 🔧 零配置混合检索
BM25 trigram（零延迟兜底） + BGE-M3 向量 + Reranker 重排序 + 召回漏斗相关性排序。向量服务超时自动热切换到本地全文搜索。

## 技术栈

- **运行时**：Python 3.12+、FastAPI、Uvicorn
- **记忆内核**：mem0 v2.0.5
- **向量存储**：Qdrant（通过 qdrant-client）
- **结构化数据**：SQLite（facts.db、observations.db、scenes.db）
- **全文搜索**：SQLite FTS5 + trigram 分词器
- **向量化**：可配置（兼容 OpenAI Embedding API）
- **重排序**：可配置（兼容 OpenAI Rerank API）
- **大模型**：兼容任何 OpenAI 格式的 API

## 配置说明

aiduMEM 从 `mem0_config_local.json` 读取配置。主要字段：

```json
{
  "llm": {
    "provider": "openai",
    "config": {
      "model": "你的模型",
      "api_key": "你的密钥",
      "base_url": "你的接口地址"
    }
  },
  "embedder": {
    "provider": "openai",
    "config": {
      "model": "BAAI/bge-m3",
      "api_key": "你的密钥",
      "base_url": "your-embedding-endpoint"
    }
  },
  "vector_store": {
    "provider": "qdrant",
    "config": {
      "collection_name": "aidu_mem",
      "host": "localhost",
      "port": 6333
    }
  }
}
```

## 环境变量

v14 Aegis 起，所有与部署环境相关的可变项都通过环境变量注入，**全部可选**——不设置就走安全默认值。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AIDUMEM_HOME` | 仓库根（`__file__` 自动解析） | 覆盖仓库根目录 |
| `AIDUMEM_DATA_DIR` | `<repo>/data` | 数据库与向量库落盘位置 |
| `AIDUMEM_LOG_DIR` | `<repo>/logs` | 日志目录 |
| `AIDUMEM_CONFIG_FILE` | `<repo>/mem0_config_local.json` | mem0 配置文件路径 |
| `AIDUMEM_DEFAULT_USER_ID` | `default` | 默认 user_id |
| `AIDUMEM_DEFAULT_AGENT_ID` | `default` | 联邦默认 agent_id |
| `AIDUMEM_DEFAULT_AGENT_NAME` | `Default Agent` | 联邦默认 agent 显示名 |
| `AIDUMEM_API_BASE` | `http://127.0.0.1:8767` | 脚本调用 API 的基址 |
| `AIDUMEM_SERVICE` | `aidumem-api` | systemd 服务名（升级检查脚本用） |
| `AIDUMEM_ENTITY_KEYWORDS` | 空 | 相关性闸门的自定义实体词表，`\|` 分隔，如 `Alice\|Bob\|ProjectX` |
| `AIDUMEM_SERVER_KEYWORDS` | 空 | 运维类查询的自定义关键词 |
| `AIDUMEM_DATE_KEYWORDS` | 空 | 日期类查询的自定义关键词 |
| `AIDUMEM_L0_CATEGORIES` | `铁律,暗号,认证` | 零衰减（L0）category 白名单 |
| `AIDUMEM_L1_PREFIXES` | 空 | 按前缀归入 L1 的 category |
| `AIDUMEM_HOST_STATE_DB` | 空 | 宿主 Agent 的 state.db，用于自动记忆抽取；不设则跳过 |
| `AIDUMEM_HOST_MEMORY_MD` | 空 | 宿主 MEMORY.md 路径，用于 `mem0_sync.py` |
| `AIDUMEM_ROUTER_*` | 空 | 可选的上游 LLM 网关用量采集（见 `ducky/router_usage.py` 头注释） |

例：把数据放到独立盘、自定义实体词表

```bash
export AIDUMEM_DATA_DIR=/data/aidumem
export AIDUMEM_ENTITY_KEYWORDS="Alice|Bob|ProjectX"
python api_server.py
```

## 路线图

- [ ] MCP（Model Context Protocol）服务端模式
- [x] 多 Agent / 多 Profile 联邦记忆（v13.0 Pantheon ✅）
- [x] 零硬编码可移植部署（v14.0 Aegis ✅）
- [ ] 跨机器联邦（HTTP 对端拉取，非同库）
- [ ] 多用户工作区隔离
- [ ] 记忆整合仪表盘
- [ ] 自定义衰减曲线插件系统
- [ ] REST API → GraphQL 适配器

## 参与贡献

欢迎贡献！请先开一个 Issue 讨论你想做的改动。

## 许可证

MIT 许可证 — 详见 [LICENSE](LICENSE)。

---

<p align="center">
  <sub>思考版本 · Aegis·埃癸斯 | 由 <a href="https://github.com/monkey2jack">monkey2jack</a> & <a href="https://github.com/Aowen-Nowor">Aowen</a> 构建</sub>
</p>
