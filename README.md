<p align="center">
  <img src="assets/aidumei-v20-banner.svg" alt="aiduMEI v20.3" width="100%">
</p>

# aiduMEI⚕爱嘟优忆思——智能体通用智慧引擎

> **aidu Memory Engine Insight**
>
> *不只是记忆 — 是洞察。*
>
> *记忆不是记事，而是不忘过往的点点滴滴；*
> *洞察不是看见，而是看懂每一条记忆为何被想起；*
> *引擎不是工具，而是让 AI 会记忆、会思考、会进化。*

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10–3.12-yellow.svg)](https://www.python.org/)
[![Built on mem0](https://img.shields.io/badge/built%20on-mem0-orange.svg)](https://github.com/mem0ai/mem0)

**中文** | **[📖 English](README_EN.md)** | **[🤖 Agent Guide](AGENTS.md)**

---

## 🎯 一行 Prompt，全自动部署

把下面这一行发给你的 AI Agent（**正典全文见 [prompts/install.txt](prompts/install.txt) —— 分步 13 行版**；上一版这里展示的是另一段旧文案却声称与 canonical 逐字一致，九份审计 P0-8 已收敛，acceptance 对账焊死）：

```text
请从官方仓库安装 aiduMEI，并严格读取 AGENTS.md：按其中 11 步逐步部署、每步以脚本退出码与 JSON 证据判定、失败即停修复重试，最终以 report.py 输出机器可读的完整汇报。
```

> 这一行只是**入口指引**；部署正典 = [prompts/install.txt](prompts/install.txt)（canonical，复制其全文给你的 Agent 即可）。详细说明见 [AGENTS.md](AGENTS.md)。

---

## aiduMEI 是什么？

**aiduMEI**（爱嘟优忆思，aidu Memory Engine Insight）是一个**智能体通用智慧引擎**（AI Wisdom Engine）—— 为 AI Agent 提供持久化记忆、推理与**可视化洞察**能力。它承载着一套完整的**认知架构**，让 AI **会记忆、会思考、会进化**，并通过自带的**控制台**让一切可见、可调、可追溯。

> **当前公开正式版 v20.3 —— 优忆思：一行 Prompt 全自动部署 · 双引擎自动挡 · 市面独一份。**

<!-- distribution-policy: github-source-only -->
> **分发说明（GitHub-only）**：aiduMEI 不再通过 PyPI 或 GHCR 发布和维护安装包。请从本仓库的
> `main` 分支获取持续更新，或从 [GitHub Releases](https://github.com/monkey2jack/aiduMEI/releases)
> 获取正式版本。
> 下方的 `pip install -r requirements.txt` 仅表示在已克隆的源码目录中安装依赖，不是 PyPI 安装方式。

> **品牌演进**：aiduMEM（优忆思）→ aiduMEI⚕爱嘟优忆思。从一个记忆中间件，升级为带可视化洞察的智能体通用智慧引擎。"爱嘟"是用户与 AI 助手的亲密呼唤，"优忆思"是记忆·思考·洞察的三重承诺。

基于 [mem0](https://github.com/mem0ai/mem0) 构建，aiduMEI 在其之上搭建了逐版生长的认知体系：

| 层级 | 代号 | 做什么 | 核心特性 |
|------|------|--------|----------|
| 🦉 **智慧** | Athena 雅典娜 | 记完之后如何变聪明 | Reflect 主动反思 · 记忆自编辑去重 · 递归精炼 · Skill 自生长 · 人格记忆基座 |
| 🧠 **回忆** | Mnemosyne 谟涅摩绪涅 | 在对的时间找到对的回忆 | Ebbinghaus 遗忘曲线 + BM25/trigram + 向量混合检索 |
| 🔍 **闸门** | Tahoe-Gate | 只检索真正相关的内容 | 启发式闸门（`GET /gate`）拦截无关上下文 —— 闲聊跳过检索，省 Token 与算力 |
| 🌊 **潮浪** | Mnemosyne Tidal | 批量 LLM 提取，不逐条调用 | 异步合并队列：多条短消息 → 单次 LLM 调用 |
| ⏳ **遗忘** | Ebbinghaus Decay | 遗忘是特性，不是 bug | 三轨衰减：Identity 零衰减 / Emotion 加速半衰 / 一般事实标准曲线 |
| 🕰️ **克罗诺斯** | Chronos 克罗诺斯 | 时间感知的有效期 | 双时间轴（valid_from / valid_to），过期降权不删除 |
| 🏛️ **万神殿** | Pantheon 万神殿 | 多 Agent 共享一套记忆 | 联邦身份 + MoE 门控 + 四级无缝降级 |
| 🛡️ **埃癸斯** | Aegis 埃癸斯 | 零硬编码，换机即跑 | 身份/路径/词表全部环境变量注入，克隆即用 |
| 🌈 **伊里斯** | Iris 伊里斯 | 走宿主官方记忆通道 | Hermes MemoryProvider 插件：压缩前抢救 · 记忆镜像 · 工具直连 |
| 🐙 **八爪鱼** | Opus Octopod | 记忆治理与结晶 | ConflictResolver 冲突消解 + TreeMemory 树状图谱 + SkillCrystallizer 自动结晶 |
| ⚡ **宙斯** | Zeus 宙斯 | 吸星大法 · 众神之王 | Raw Drawer 原味抽屉 + Code Graph 代码图谱 + EvolveMem 检索自进化 + **多模态视觉记忆 · Obsidian 双链 · 无损秒级升级** |

---

## 🚗 智慧引擎自动挡（v20.3 正式实现）

**一台引擎，三种挡位，自动换挡** —— 市面上第一个把「本地备胎」做成全链路自动降挡的开源记忆系统。
但我们不强迫你接受它：**灵活多变任你选，实事求是报消耗。**

### 灵活多变任你选

挡位是**你的部署选择**，不是我们替你做的假设。`.env` 里一行，三种活法：

| | ☁️ 云端档 `cloud` | ⚙️ 自动挡 `auto`（默认） | 🔋 本地档 `local` |
|---|---|---|---|
| **语义召回** | 云端嵌入全性能 | 云为主，断供**自动切本地** | 纯本地 ONNX（512 维小模型） |
| **记忆蒸馏** | LLM 蒸馏 | LLM 蒸馏，断供转确定性抽取 | 确定性抽取，**零 LLM** |
| **外部服务挂了** | 无备胎，如实判 `degraded` | **无感续跑**，恢复自动切回 | 没有外部服务可挂 |
| **Token 消耗** | 正常 | 正常（断供期为零） | **恒为零** |
| **要不要密钥** | 要 | 要（缺了就恒跑本地） | **一个都不要** |
| **切换方式** | `AIDUMEI_ENGINE_MODE=cloud` | 默认，或 `=auto` | `=local` |

三档共用**同一套数据与契约**——切档不迁移、不重建索引、不改调用方代码。
两条腿（云腿 / 本地腿）是**独立开关**而不是三选一的死结构：将来要加第四种形态，
调用点一行都不用改。

### 实事求是报消耗

「省内存」和「让你自己选」在我们这儿是**同一件事**，因为我们先老老实实量过：

| | 云端档 | 自动挡 / 本地档 | 差额 |
|---|---|---|---|
| 运行内存 | **约 280 MB** | 约 430 MB | **151 MB** |
| 依赖磁盘 | 约 275 MB | 约 353 MB + 模型 91 MB | 约 169 MB |

这 151 MB 的去向也摊开讲：**onnxruntime 运行库本身 import 就吃 75 MB，模型会话与权重约 122 MB。**
我们试过压它——`threads=1`、ONNX arena 按需分配、`malloc_trim`、`MALLOC_ARENA_MAX=2`，
**四种旋钮实测全部无效**（206~215 MB，在噪声范围内）；模型也已经是能用的中文模型里最小的一档。
所以我们**没有假装优化，而是把它做成了开关**：不需要备胎，那 151 MB 一分不花。

> 备胎为什么会常驻、而不是「断供时才加载」：双索引要求**每一次写入**都同步算一份本地向量——
> 不写就没数据，等断供那一刻再加载模型也召回不到任何东西。**备胎是提前备好的，不是临时找的。**
> 这是设计取舍，写在这里让你自己判断值不值。完整实测（含冷启动、延迟、CPU）见下方「部署要求」章。

### 自动挡到底怎么工作

- **自动降挡**：嵌入服务连续失败触发熔断切换；且**单次查询内就地换腿**——云腿当场炸掉，这一次请求就落到本地索引，无感顺滑，不是下一个用户才享受降挡。
- **自动升挡**：半开探测拿真实流量试探，连续成功才回云挡（假恢复骗不动挡位）；降挡期间欠下的 LLM 蒸馏自动重放补算，一条不丢，重启也不赖账。
- **LLM 蒸馏腿同样有挡位**：蒸馏服务断供时写入秒级降为确定性直写——原文、硬事实、云向量照落，内容照样可召回，欠的只是蒸馏精修且账目可查；传输层盲重试已掐（网关的 `Retry-After` 不再把单次写入挂几分钟）。实测断供期单次写入 **4.5 分钟 → 0.15 秒**。
- **挡位诚实**：`/search` 响应带 `engine_mode`（按本次实际用的腿报告，不是按系统挡位）；lite 分数口径明示；升降挡进事件账本——哪段时间跑在备胎上，审计可查。`/health` 有 `engine_mode_policy` / `engine_gear` / `llm_gear` 三个探针，被配置关掉的腿**如实报 `disabled_by_policy`**，绝不假装在服役。
- **生产实测，不是设计稿**：断供演练在真实生产环境全链验证——封锁端点 → 三连失败自动降挡 → 断供期写入照落且**语义可召回**（`vector_leg=local` 实证）→ 恢复自动升挡 → 欠账消化归零。演练期间还真撞上两场外部网关故障，机制照样接住。
- **诚实标注短板**：lite 是保命档不是平替——20 条真实查询对照，本地小模型与云模型的 top5 排序重叠约 9%（2026-08-26 实测，样本 20 条，指标为 top5 Jaccard 重叠率；口径含原文向量稀释与模型排序分歧）。**断供时搜得到该搜到的，排序品质明写不如云挡。**
- **契约差异也写清**：lite 挡的 `/add` 受理响应是写路径契约（`status` / `action` / `engine_mode`），**不含** `/search` 的召回判语字段族——欠账受理不是召回，调用方勿按 `/search` 契约解析。

裸装（不配任何云服务密钥）时它天然一直跑在本地档——**开箱即用的零依赖记忆库**；配上密钥自动升挡。
一个包，三种活法，你说了算。


> 三轮安全外审与社区 issue 的完整账本见 [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md)：只保留结论，过程与证据移出主 README。

> 竞品定位、版本谱系与架构演进见 [docs/POSITIONING.md](docs/POSITIONING.md)、[docs/VERSION-LINEAGE.md](docs/VERSION-LINEAGE.md)。

## 一行 Prompt 部署

把下面这段复制给你的 AI Agent（Claude Code、Cursor、Codex 等），它会自动完成全部部署和验证：

```text
请从官方仓库安装 aiduMEI，并严格读取 AGENTS.md：自动检查本机环境、选择最稳妥部署路径与 cloud/local/auto 挡位，完成配置、服务启动、e2e 生效验证、宿主记忆接入、维护任务初始化和 report.py 自检报告；每一步只以脚本退出码和 JSON 证据判定，遇到失败立即停止、修复并重试，最终向我汇报版本、挡位、健康、水位、召回质量、维护状态及未关闭风险。
```

<details>
<summary>📋 手动安装（不用 Agent 的话点这里展开）</summary>

```bash
git clone https://github.com/monkey2jack/aiduMEI.git && cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 如需自动/本地挡位：
pip install .[local-embed] && python scripts/fetch_local_embed_model.py
# 配置：
cp mem0_config_local.json.example mem0_config_local.json
cp .env.example .env
# 编辑 mem0_config_local.json 填入 LLM 和 Embedding Key
# 编辑 .env 填入 AIDUMEM_ENTITY_KEYWORDS 和 AIDUMEM_API_TOKEN
# 启动：
python api_server.py
# 验证：
python scripts/e2e_smoke.py --json
```

</details>

> 💡 `AIDUMEM_ENTITY_KEYWORDS` 让相关性闸门认得你自己的人名/项目代号，用 `|` 分隔，重启即生效。
> 📖 详细部署文档：[AGENTS.md](AGENTS.md) · 宿主接入：[docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)

---

## 📦 部署要求——两种体量，自己选

> 大家关心的问题：这套东西部署起来重不重？**答案取决于你选哪个挡位。**
> v20.2 的双引擎自动挡带来了本地备胎，备胎要占内存——所以我们把选择权交给你，
> 并且**把两种体量的实测数字都摆在这里**（2 核 3.5G 云主机 · 2026-08-27 实测）。

| 维度 | ☁️ 云端档（`cloud`） | ⚙️ 自动挡（`auto`，默认） | 🔋 本地档（`local`） |
|------|---------------------|--------------------------|---------------------|
| **运行内存** | **约 280 MB** | **约 430 MB** | 约 430 MB |
| **依赖磁盘** | 约 275 MB | 约 353 MB + 模型 91 MB | 同自动挡 |
| **断供时** | 无备胎，如实判 `degraded` | **自动降挡续跑** | 不依赖外部，无所谓断供 |
| **Token 消耗** | 正常 | 正常（断供期为零） | **恒为零** |
| **需要密钥** | 需要 | 需要（缺了就恒跑本地） | **不需要** |

**共同项**：CPU 2 核足够、闲时 < 1%；`/search` 单次 0.14~0.23s；冷启动 5.2s；
数据盘千级记忆约 13 MB 向量 + 数百 KB SQLite；前端 0 依赖；Python 3.10–3.12（推荐 3.12）。

**那 150 MB 花在哪、能不能省**（实测，不是估算）：

| 项 | 内存 |
|---|---|
| onnxruntime 运行库本身（只 import 不加载模型） | **75 MB** |
| bge-small-zh-v1.5 会话与权重 | **约 122 MB** |
| 服务在两种档位下的实测差 | **151 MB** |

我们试过压它：`threads=1`、ONNX arena 按需分配、`malloc_trim`、`MALLOC_ARENA_MAX=2`
——**四种旋钮实测全部无效**（206~215 MB，在噪声范围内）；模型也已经是
fastembed 目录里**最小的中文可用款**（0.09 GB，次小的多语言方案是它的 2.4 倍）。
所以我们没有假装优化，而是给了你开关：**不需要备胎就选云端档，那 151 MB 一分不花。**

> **为什么备胎会常驻，而不是「断供时才加载」**：双索引要求**每一次写入**都同步
> 算一份本地向量——不写就没数据，等断供那一刻再加载模型也召回不到任何东西。
> 备胎是提前备好的，不是临时找的。这是设计取舍，写在这里让你自己判断值不值。

**怎么选**（`.env` 里一行）：

```bash
AIDUMEI_ENGINE_MODE=auto    # 默认：云端为主，断供自动切本地，恢复自动切回
AIDUMEI_ENGINE_MODE=cloud   # 省内存：只用云端，不装/不加载本地模型
AIDUMEI_ENGINE_MODE=local   # 零 token、零外部网络、不需要任何密钥
```

云端档还可以更彻底：**不安装可选依赖组** `local-embed`（即不装 `fastembed`），
连那 91 MB 模型都不必下载。装了但选云端档也不会加载——开关在运行时也生效。

**其余的轻，仍然是刻意设计：**

- **向量库嵌入式落盘，不起独立服务**：Qdrant 走 `path: ./data/qdrant` 本地模式，无独立进程、无 Docker、无额外端口。
- **不吃 GPU**：本地备胎是 512 维小模型的 ONNX CPU 推理，不需要显卡；云端档下连它也没有。
- **相关性闸门先拦一道**：日常闲聊不触发检索，Token 与算力消耗省掉一个量级。
- **SQLite + FTS5 兜底**：结构化知识与全文搜索用零依赖的 SQLite。

> 一句话：**云端档 1 核 1G 跑得动；自动挡/本地档建议 2 核 2G 起步。**
> 早先版本这里写的是「约 210 MB / 1 核 1G」——那是自动挡之前的数字，
> 已按实测更正（宣称即承诺，见下方「测试与质量」一节的同款纪律）。

---

> 竞品定位与跑分态度见 [docs/POSITIONING.md](docs/POSITIONING.md) 与 [docs/BENCHMARKING-POSTURE.md](docs/BENCHMARKING-POSTURE.md)；完整版本谱系见 [docs/VERSION-LINEAGE.md](docs/VERSION-LINEAGE.md)。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│           aiduMEI⚕爱嘟优忆思 v20.3            │
│              FastAPI REST API :8767                       │
│              控制台 /ui :8767（自带静态托管）              │
│              MCP Server :8766 (41 tools)                  │
├──────────────────────────────────────────────────────────┤
│  v19.3 Engine    → 注入防护 · WAL多仓级联 · 统一打分 · 动态健康
│  Athena          → Reflect反思 · 自编辑 · 精炼 · Skill生长 · 人格基座 │
│  Core (HOT)      → 搜索、添加、CRUD、健康检查              │
│  v8 Pipeline     → 点火 · 工作区 · 广播 · 镜鉴 · 会话      │
│  Clotho/Hyperion → CoreMemory · 检查点 · AutoDream       │
│  Extended        → 15脉外延：自动记忆 · 过期 · 统计        │
│  Federation      → 多 Agent 联邦 · MoE 门控 · 四级降级     │
│  Octopus         → 冲突消解 · 树状记忆 · 技能结晶          │
│  Zeus            → 原味抽屉 · 代码图谱 · 检索自进化         │
│  Themis          → 事件账本 · 敏感分档 · 治理审计          │
│  aiduMEI 控制台  → PULSE · VAULT · MAP · RECALL · EVOLVE · SETTINGS │
├──────────────────────────────────────────────────────────┤
│  mem0 (向量记忆) + Qdrant (向量存储)                       │
│  facts.db (结构化知识 · FTS5 trigram 全文搜索)             │
│  EvolveMem 检索自进化引擎 (后台自动衰减/提权)               │
└──────────────────────────────────────────────────────────┘
```

---

## 核心接口

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/add` | Write memory; response `action` states direct/async/coalesce behavior |
| `POST` | `/add/raw` | Store raw text without LLM distillation |
| `POST` | `/search` | Hybrid recall with verdict, confidence, engine mode, and trace fields |
| `POST` | `/search_trace` | Recall with full funnel trace |
| `DELETE` | `/delete?memory_id=…` | Delete by ID; response is `committed` / `partial` / `failed` / `not_found` |
| `POST` | `/delete_all` | Clear one `(user_id, bank_id)` scope; same response contract |
| `GET` | `/health` | Runtime probes and actual paths |
| `GET` | `/gate` | Decide whether a turn needs memory retrieval |
| `GET` | `/stats` | Scope-scoped memory statistics |
| `POST` | `/api/core-memory/inject` | Inject core memory context |
| `POST` | `/facts/inject-context` | Inject fact context |
| `POST` | `/session/start` / `/session/end` | Session lifecycle |
| `GET` | `/metrics` | JSON metrics (not Prometheus format) |

Write and search must use the same `user_id` and `bank_id`. `/search` returns an empty query verdict instead of random memories. Delete responses distinguish failed layers from intentionally exempt layers. A configured-but-broken mem0 backend is a real failure; only the typed initialization signal for a never-configured backend may skip that layer.

## 接入 Hermes Agent

| 方式 | 能力 | 何时用 |
|------|------|--------|
| **A. MemoryProvider 插件**（推荐） | 全生命周期钩子 + 工具 + 备份 | 默认选这个 |
| **B. Shell Hook** | 仅 turn 开头注入 | 宿主不方便装插件时 |

两种方式**不要同时开**（会重复注入白烧 token）。完整步骤、验证方法与回滚见 [integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md)。

> ⚠️ **安全**：默认只监听 `127.0.0.1`；设置 `AIDUMEM_API_TOKEN` 或 UI 口令后接口会强制鉴权。跨机访问请配置凭据并前置 TLS 反代，别把无凭据实例暴露到公网。
>
> ⚠️ **会话是进程内的**（单机自托管形态下的有意取舍）：服务重启后所有登录会话失效，需要重新登录；**多实例部署时会话不共享**——同一个用户被负载均衡打到另一个实例上会被要求重新登录，那不是 token 坏了。真要多实例，请在反代上做会话粘滞（sticky session）。

---

## MCP Server（41 工具 · 默认端口 8766）

aiduMEI 内置 MCP Server（`:8766`），暴露 41 个工具，分组如下：

| 工具组 | 数量 | 说明 |
|--------|------|------|
| Core CRUD | 6 | add / search / delete / update / recent / stats |
| Facts | 4 | facts_add / facts_search / facts_list / facts_delete |
| Code Graph | 2 | code_impact / code_graph |
| Session | 2 | session_list / session_history |
| Reflect | 2 | reflect_recent / reflect_trace |
| Core Memory | 3 | core_memory_get / core_memory_set / core_memory_list |
| AutoDream | 2 | dream_trigger / dream_status |
| Raw Drawer | 2 | raw_add / raw_search |
| Knowledge Tree | 3 | tree_nodes / tree_node / tree_ancestors |
| Crystals | 3 | crystals_list / crystals_detect / crystals_approve |
| Conflict | 1 | conflict_resolve |
| Evolve | 2 | evolve_feedback / evolve_report |
| Federation | 6 | fed_recall / fed_add / fed_agents / fed_register / fed_broadcast / fed_awareness |
| Persona（v19.0） | 3 | persona_build / persona_retrieve / persona_banks |

---

## IDE 集成

### Cursor

```bash
# 将规则文件复制到项目
cp integrations/cursor-hook/cursor-aidumem.mdc .cursor/rules/

# 文件保存时自动存入 Raw Drawer
cp integrations/cursor-hook/aidumem-on-save.sh .git/hooks/post-commit
```

### Claude Code

```bash
python integrations/cursor-hook/claude-code-hook.py store --file my_code.py
python integrations/cursor-hook/claude-code-hook.py search --query "database connection"
python integrations/cursor-hook/claude-code-hook.py impact --file ducky/utils.py
```

---

## 技术栈

- **运行时**：Python 3.10–3.12（推荐 3.12）、FastAPI、Uvicorn
- **记忆内核**：mem0 v2.0.19（v20.3）
- **向量存储**：Qdrant（通过 qdrant-client）
- **结构化数据**：SQLite（facts.db、observations.db、scenes.db、fact_events.db）
- **全文搜索**：SQLite FTS5 + trigram 分词器
  - **中文切词策略（v19.4.1 P1-2 更正）**：trigram 分词器索引的是 3 字符窗口，因此中文查询按 **3-gram** 切词才能命中索引。v19.4.0 及之前切 2-gram，与索引失配——中文查询实际一直落在 `LIKE` 全表扫描上（20 万条原文实测稀有词 32.8 ms）。现已对齐，同量级降至 0.05 ms。
  - **trigram 的固有边界**：不足 3 字的查询（如「祖母」）无法用 trigram 表达，由 `LIKE` 兜底。这是分词器定义决定的，不是缺陷；召回结果的 `_recall_path` 字段（`fts` / `like`）会如实标注本次真走的哪条路，降级不再静默。
- **向量化**：可配置（兼容 OpenAI Embedding API）
- **重排序**：可配置（兼容 OpenAI Rerank API · 多 provider 抽象：OpenAI-compatible / Jina / Cohere）
- **大模型**：兼容任何 OpenAI 格式的 API
- **MCP**：fastmcp stdio + HTTP 双模
- **控制台**：零构建纯静态（HTML + CSS + 原生 JS + 本地 ECharts 资产），由后端 `/ui` 直接托管

---

## 配置说明

aiduMEI 从 `mem0_config_local.json` 读取配置。主要字段：

```json
{
  "llm": {
    "provider": "openai",
    "config": {
      "model": "你的模型",
      "api_key": "你的密钥",
      "openai_base_url": "你的接口地址",
      "is_reasoning_model": false,
      "reasoning_effort": "none"
    }
  },
  "embedder": {
    "provider": "openai",
    "config": {
      "model": "your-embedding-model",
      "api_key": "你的密钥",
      "openai_base_url": "你的接口地址"
    }
  },
  "rerank": {
    "enabled": true,
    "provider": "openai_compatible",
    "config": {
      "model": "你的重排模型",
      "api_key": "你的密钥",
      "openai_base_url": "你的接口地址"
    }
  },
  "vector_store": {
    "provider": "qdrant",
    "config": {
      "path": "./data/qdrant",
      "embedding_model_dims": 1024
    }
  }
}
```

> 💡 LLM 的 `is_reasoning_model: false` + `reasoning_effort: "none"` 是刻意写死关闭的——记忆提取需要快速直答，不需要深度推理。控制台 SETTINGS 面板的"思考模式"区块只读展示这一状态。

---

## 环境变量

v14 Aegis 起，所有与部署环境相关的可变项都通过环境变量注入，**全部可选**——不设置就走安全默认值。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AIDUMEM_HOME` | 仓库根（`__file__` 自动解析） | 覆盖仓库根目录 |
| `AIDUMEM_DATA_DIR` | `<repo>/data` | 数据库与向量库落盘位置 |
| `AIDUMEM_LOG_DIR` | `<repo>/logs` | 日志目录 |
| `AIDUMEM_CONFIG_FILE` | `<repo>/mem0_config_local.json` | mem0 配置文件路径（由 `AIDUMEM_HOME` 推导，固定文件名） |
| `AIDUMEM_DEFAULT_USER_ID` | `default` | 默认 user_id |
| `AIDUMEM_DEFAULT_AGENT_ID` | `local` | 联邦默认 agent_id |
| `AIDUMEM_LEGACY_USER_IDS` | 空 | 历史 user_id 映射（逗号分隔，如 `admin,user`），映射后老数据才能被召回。v19.1.1 起不再硬编码 `admin/user` 映射 |
| `AIDUMEM_API_TOKEN` | 空 | REST API 访问令牌；设置后所有接口强制 `Authorization: Bearer`。本地/回环可不设，对外部署必设 |
| `AIDUMEM_API_PORT` | `8767` | API + 控制台监听端口 |
| `AIDUMEM_ENTITY_KEYWORDS` | 空 | 相关性闸门的自定义实体词表，`\|` 分隔 |
| `UI_DIR` | `<repo>/frontend` | 控制台静态文件目录（不存在则仅 API 模式） |
| `AIDUMEM_URL` | `http://127.0.0.1:8767` | 宿主插件 / shell hook 访问服务的地址 |
| `AIDUMEM_USER_ID` | `default` | 宿主插件 / hook 使用的记忆命名空间 |
| `AIDUMEM_MIN_HISTORY` | `6` | shell hook：会话历史少于这个条数就不注入 |
| `AIDUMEM_CONFIG_READONLY` | `0` | 控制台配置只读模式（1=禁止在线改配置） |

完整清单连注释见 [`.env.example`](.env.example)，`cp .env.example .env` 起步。

---


## 测试与质量

```bash
# 全量回归
pytest tests/
# 编译检查
python -m compileall ducky api_server.py mcp_server.py
```

**测试层级如实说明（v19.4.1 P3-3）**
> 读表口径：每行「通过 · 跳过」相加等于**该形态、该日期**下 `pytest --collect-only` 的收集数；不同日期的行分母可以不同（树在长），以各行括注的日期为准。跳过数按「轴」解释（下方跳过轴表），不是失败。

| 维度 | 现状 |
|------|------|
| 用例总数 | **1728**（`pytest --collect-only` 实测，2026-09-03） |
| 独立开发机 | 1716 通过 · **12 跳过** —— 缺宿主 Hermes 源码，有 git 工作区（**2026-09-03 实测**，v20.3.2 正式版树） |
| 基础安装路径 | 1696 通过 · **32 跳过** —— 只装 `requirements.txt` + `requirements-dev.txt`，**这是新用户实际会得到的数**（**2026-09-03 干净 venv 实测**，Python 3.12） |
| 生产机沙箱 | 1723 通过 · **5 跳过** —— **2026-09-03 生产机实测**（bundle clone 含 `.git`，不带 `.env`；跳过 = ruff×3 + mcp×2，与按轴推导值逐条吻合；备胎模型轴门控用例在此形态实际不跳） |
| 全轴齐备 | 1728 通过 · **0 跳过** —— **2026-09-03 生产机实测**（五轴齐备：`.git` + `ruff` + `mcp` extra + 宿主源码 + 备胎模型缓存；独立全轴 venv，不带 `.env` 其余项） |
| 层级 | 以**模块级单元测试 + 源码级守卫断言**为主，`TestClient` 驱动的接口测试为辅 |
| 平台前提 | 全量套件按 **Linux/macOS（POSIX）**口径维护：`backup_gate` 轴要 POSIX shell；`/health` 的 CPU/RSS 指标走 `resource` 模块，非 POSIX 平台诚实置 `None` 不崩（v20.1 整改）。Windows 未列为全量测试平台 |
| 语句覆盖率 | 约 51%（`ducky/` + 入口，`coverage` 实测） |
| 未覆盖 | 真实 mem0 / Qdrant 集成、真实 LLM 调用、并发压测 —— 这些依赖外部服务，由生产环境实机冒烟承担 |

### 环境矩阵：同一套测试，五种环境，差异逐条归因（2026-08-29 实测）

> **为什么要做成矩阵。** v20.2.5 里有四条测试**在沙箱绿、在部署机红** —— 唯一的变量是
> 重排服务可不可达（沙箱拿不到凭据 → 降级保分；部署机凭据齐全 → 真融合，分数就变了）。
> **一个测试的结论如果取决于外部服务今天在不在，它就不是判据，是天气预报。**
> 只报一个环境的数字，等于把这类缺陷藏起来。

| # | 环境 | 通过 | 跳过 | 跳过归因 |
|---|------|-----:|-----:|---------|
| ① | 独立开发机 · 完整 extras | 1716 | 12 | 宿主 Hermes 源码缺席 ×12（2026-09-03 实测，v20.3.2 正式版树） |
| ② | 干净克隆 · **无配置** · 有 `.git`（≈ 第一次拿到本项目的人） | 1497 | 2 | `ruff` 未安装 ×2 —— **2026-08-29 基线（总数 1499 时代）**，已被 ④⑤ 取代 |
| ③ | 干净克隆 · **带生产配置** · 有 `.git`（重排可达） | 1497 | 2 | `ruff` 未安装 ×2 —— **2026-08-29 基线（总数 1499 时代）**，已被 ④⑤ 取代 |
| ④ | 生产机沙箱 · 宿主源码 · 生产 venv · 不带 `.env` | 1723 | 5 | 2026-09-03 实测（v20.3.2 正式版树；ruff×3 + mcp×2） |
| ⑤ | 全轴齐备 · 有 `.git` · `ruff` · `mcp` extra · 宿主 · 备胎模型缓存 | **1728** | **0** | 2026-09-03 生产机实测（五轴齐备，独立全轴 venv） |

已测行满足 `通过 + 跳过 = 1728`（②③ 是总数 1499 时代的基线，已标注日期）；未实测的行必须重新实测后填数，不许沿用旧日期改数字 ——
**归因不了的差异，就是还藏着一条「换个环境才现形」的缺陷。**

**② 与 ③ 数字完全相同，这一格是重点。** 两者唯一的差别就是重排服务可不可达；
在 v20.2.5 那一版，③ 这一格是 **4 failed**。这条差异消失，是「测试不再依赖外部服务
当天的状态」的**证据**，不是一句自我保证。

做法：断分数的用例默认把重排摘掉（判据只由打分公式决定），另有一组用可控替身把重排
打开、断言闸门在融合后依然正确 —— **两种环境都验，不是二选一**。再加一条元守卫：
凡是拿打分输出做断言的用例必须显式声明重排状态，否则红。那条守卫上线当场抓出一个存量
用例（它今天两边都过，纯属排序恰好没被融合分改变）。

> **⚠️ 这些数字对应「装齐可选依赖」的环境**（v20.2.5 补记，外审指出的口径缺口）。
>
> 上表的 1728/1716/12 跑在完整环境下：`regex`、`nltk`、`numpy`、`qdrant_client`、
> `mem0ai`、`fastembed` 都在场。而 README「30 秒上手」教的基础路径只装
> `requirements.txt` —— 那些可选依赖不在，对应的跳过轴会**一起跳掉**，
> 于是 passed 更少、skipped 更多。第三方外审在基础路径下实测到的是
> **1415 passed · 27 skipped**（他们的 Python 3.14 环境）。
>
> **两个数字都是真的，差别只在环境。** 之前只写了一套，读者按 README 装完
> 跑出别的数会以为哪里错了 —— 这是口径没写清，不是数字造假。复现命令：
>
> ```bash
> # 完整环境（上表那一套）
> uv sync --all-extras && uv run pytest tests/ -q
> # 基础路径（README「30 秒上手」教的那条）
> pip install -r requirements.txt && pip install pytest pyyaml && pytest tests/ -q -rs
> ```

> **为什么要把 1716 和 1723 都写出来**：同一份测试集在不同环境下跑出不同数字，只报其中一个都会误导读者。
> **跳过不止一条轴**（v20.0 实测补正）：此前这一段只认「宿主 Hermes 源码」一条轴，于是把「全绿」
> 当成了装上宿主就能拿到的东西。生产实跑打脸 —— 沙箱里宿主明明在场，跑出来**仍有 1 条跳过**。
> 全量普查后，跳过其实有**十一条互不相干的轴**（v20.1 补第十条 mem0 基座；v20.2 补第十一条 fastembed 备胎）：
>
> | 跳过轴 | 门控用例数 | 位点 |
> |--------|-----------|------|
> | 宿主 Hermes 源码 | 12 | `tests/test_hermes_plugin.py` 整份 |
> | git 工作区 | 1 | `tests/test_v20_brand_policy.py`（要 `git ls-files` 当比对基准） |
> | `scripts/backup_gate.sh` + POSIX shell | 8 | `tests/test_v19_4_1_backup_gate.py` 整份 |
> | `qdrant_client` 已安装 | 1 | `tests/test_v20_vector_bank_contract.py` |
> | LoCoMo 数据集已就位 | 1 | `tests/test_v20_locomo_official.py`（全量数据集扫描要真文件） |
> | `regex` 已安装 | 1 | `tests/test_v20_locomo_official.py`（拿 `regex` 给标准库 `re` 对拍） |
> | `numpy` 已安装 | 1 | `tests/test_v20_locomo_official.py`（拿 `numpy.mean` 给 `sum/len` 对拍） |
> | `nltk` 已安装 | 13 | `tests/test_v20_locomo_official.py` 与 `tests/test_v20_benchmarks.py`（官方 F1 的 PorterStemmer，换实现就不是官方口径） |
> | `git` 可执行文件在场 | 6 | `tests/test_v20_gitignore_guard.py` 整份（拿一个临时空仓当 ignore 判据，不碰本仓的 `.git`） |
> | `mem0ai` 已安装 | 20 | `tests/test_v20_mem0_patch_layer.py` 整份（补丁层疗法要真实基座在场；此前缺 mem0 是 20 条 ERROR 冒充真缺陷，现在诚实跳过） |
> | `fastembed` 已安装 | 1 | `tests/test_v20_2_autoshift.py`（自动挡备胎真模型测试；缺依赖诚实跳过，模型未部署时用例内二次跳过） |
> | `ruff` 已安装 | 3 |
| `mcp` extra 已安装 | 2 | `tests/test_v20_2_5_audit_remediation.py`（第四道关的真缺陷类规则 F821/F811/F841；缺依赖时**诚实跳过而不是静默当成无命中** —— 第一版就是那样写的，被沙箱实测抓出：生产 venv 没有 ruff，守卫于是永远绿。push_gate 侧仍会拦） |
>
> 开发机缺第一条 → 1716 + 12；基础安装路径（只装 `requirements*`，新用户实际得到的形态）
> → **1696 + 32**（2026-09-03 干净 venv 实测）；生产机沙箱 → 1723 + 5（2026-09-03）；
> 五轴齐备（`.git` + `ruff` + `mcp` extra + 宿主 + 备胎模型缓存）→ **1728 + 0**（2026-09-03 生产机实测）。
>
> 这两行数字的来历与踩坑史见 [`docs/TESTING.md`](docs/TESTING.md)。一句话：**测的是哪棵树，就只许报哪棵树。**
>
> **这 12 条不是玄学，自己就能验**（v19.4.2 补）：它们全在 `tests/test_hermes_plugin.py`，
> 跳过条件是宿主 `agent/memory_provider.py` 找不到。`HERMES_SRC` 三态可控，**两个方向都能复现**：
>
> ```bash
> # 装齐可选依赖 —— 下面这三行命令产出的就是 12 跳过
> pip install -r requirements.txt -r requirements-dev.txt
> pip install "mcp>=1.0.0,<2" ruff nltk regex numpy fastembed
> python scripts/fetch_local_embed_model.py                       # 必须取模；运行时 HF_HUB_OFFLINE=1，只有安装包仍会多跳 1 条
> pytest tests/ -q -rs | tail -1                                 # 无宿主：1716 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # 有宿主：1728 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # 装了宿主也强制关掉，照旧 1716 passed, 12 skipped
>
> # 基础安装路径（只装 requirements*，不装任何可选组）—— 新用户实际会跑到的形态
> pip install -r requirements.txt -r requirements-dev.txt
> pytest tests/ -q -rs | tail -1                                 # 基础路径：1696 passed, 32 skipped
> ```
>
> **为什么两套都写在这里**（v20.3.2，第 10 轮外部审计 P0-3）：这一段上一版的标题是
> 「这 12 条不是玄学，自己就能验」，配的却是 `pip install -r requirements-dev.txt` ——
> 那个文件只有 `pytest` 与 `pyyaml` 两行，跑出来是 **31 条跳过**，不是 12。
> 多出的 19 条来自 bench / mcp / local-embed / ruff 四个可选组，而披露写在 60 行之外。
> **一段以「可证伪」为标题的文字，配了一条不可证伪的命令。**
> 现在命令与数字同屏，且各配各的环境。
>
> 「跳过」必须能被复现成「通过」，**反过来也必须成立**。机器上恰好装着宿主时（`/hermes/hermes-agent`
> 会被自动发现，我们自己的生产机就是这样），上面第一条命令跑出来就不是 1716 + 12 —— 2026-09-03
> 在生产机沙箱上跑出来是 1723 passed、5 skipped（2026-09-03 实测，不带 `.env`）。剩下那 5 条卡在 `ruff` ×3、
> `mcp` extra ×2 两条轴上（沙箱用生产 venv，不装 lint 工具与可选 extra；备胎模型轴门控的那 1 条在此形态实际不跳）。
> 上面代码块里的 `有宿主：1728 passed` 要**十三条轴同时齐备**才拿得到，宿主只是其中一条 ——
> 别把「装上宿主」当成「全绿」。2026-09-02 在生产机上为它单独建了一个带 `mcp` extra 的测试
> venv，五轴齐备后实测到 **1728 passed、0 skipped**。
> 没有 `HERMES_SRC=none` 这一档，读者根本无法在自己机器上把我们宣称的「12 跳过」复现出来。
> **双向可复现才叫可证伪**：一个你没法让它跳过的「跳过」，和一个你没法让它通过的「通过」，同样不可信。
>
> 另外：`HERMES_SRC` 指向的路径若不含 `agent/memory_provider.py`，会**直接报错**，
> 而不会静默回退到自动发现的路径 —— 指了 A 却在测 B 还给绿灯，是最难发现的一种假绿灯。

**为什么把这些写清楚**：v19.4.0 的 README 只写「全量测试 244 通过」，读者会理解为端到端保障。但 244 用例 0.88 秒跑完，显然不含任何真实外部依赖。更关键的是——v19.4.0 的幂等去重测试是绿的，却只覆盖了带显式时间戳的 `list[dict]` 载荷，而生产实际走的是无时间戳的纯字符串载荷，真 bug 就从这条缝里带着绿灯上线了。

因此 v19.4.1 起执行**反假绿灯纪律**：涉及载荷形态、凭据形态、查询形态的测试一律多形态并测；性能与索引类断言必须校验 `_recall_path` 这类自证字段，而不是只看「有没有命中」。

---
## 已知例外与本版不覆盖

| # | 例外 | 说明 |
|---|------|------|
| 1 | 租户隔离是按租户收窄可见性 | 非互不信任客户的硬隔离层。详见 `docs/SECURITY-AUDIT-LEDGER.md`。 |
| 2 | `evolve_mem.py` 按 5000 条全库扫描 | 记忆量增长后需优化，详见 `ducky/evolve_mem.py`。 |
| 3 | `fetch_local_embed_model.py` 必须部署期执行 | 运行时零网络；`ducky/local_embed.py` 强制 `HF_HUB_OFFLINE=1`。 |
| 4 | `capture_wave` 的 `entity_keywords` 漏配时零召回 | 无报错，需配置 `AIDUMEM_ENTITY_KEYWORDS`。详见 `ducky/pipeline/memory_gate.py`。 |

## 仓库结构

```
aiduMEI/
├── AGENTS.md              # Agent 部署/验证/运维入口
├── llms.txt               # Agent 文档索引
├── api_server.py          # 主入口（API + /ui 控制台托管）
├── ducky/                 # 业务逻辑（各神祇模块）
│   ├── hot/               #   搜索/健康/遗留端点
│   ├── pipeline/          #   相关性闸门
│   ├── speed/             #   潮浪合并/速度优化
│   ├── salience/          #   显著性/车道衰减
│   ├── federation/        #   万神殿联邦
│   ├── evolve_mem.py      #   检索自进化
│   ├── routes_config.py   #   控制台 /config 路由
│   └── ...
├── frontend/              # aiduMEI 控制台（零构建纯静态）
│   ├── index.html
│   ├── css/style.css
│   ├── js/                # api.js · panels.js · main.js
│   ├── js/vendor/         # echarts（本地随包分发，不依赖外部 CDN）
│   ├── *.png              # 六面板图标 + logo
│   └── dev_server.py      # 本地开发代理（可选）
├── tests/                 # 回归测试集（pytest）
├── tools/                 # 开发工具（截屏脚本等）
├── seed_demo.py           # 脱敏演示数据种子（虚构人物/公司）
├── seed_facts.py          # 知识树事实种子（6 域 28 条）
├── mem0_config_local.json # 模型配置（gitignored，含密钥）
└── requirements.txt
```

<p align="center">
  <sub>aiduMEI⚕爱嘟优忆思｜Powered by monkey²</sub>
</p>