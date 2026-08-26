<p align="center">
  <img src="assets/aidumei-v20-banner.svg" alt="aiduMEI v20.1" width="100%">
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
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-yellow.svg)](https://www.python.org/)
[![Built on mem0](https://img.shields.io/badge/built%20on-mem0-orange.svg)](https://github.com/mem0ai/mem0)

**中文** | **[📖 English](README_EN.md)**

---

## aiduMEI 是什么？

**aiduMEI**（爱嘟优忆思，aidu Memory Engine Insight）是一个**智能体通用智慧引擎**（AI Wisdom Engine）—— 为 AI Agent 提供持久化记忆、推理与**可视化洞察**能力。它承载着一套完整的**认知架构**，让 AI **会记忆、会思考、会进化**，并通过自带的**控制台**让一切可见、可调、可追溯。

> **当前公开正式版 v20.1 —— 确定性兜底与诚实召回：LLM 不在场时记忆系统仍是完整的记忆系统；召回给不出可信结果时，宁可诚实说「没有」。五份外部评审的 17 项整改全部闭合后发布。**
> 详细的逐版演进请看 [CHANGELOG](CHANGELOG.md)；这一页只讲**现在是什么**。

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

## 为什么 v20.0 是一次大版本

上一版 v19.5.0 和这一版，改的**不是同一层东西**。

| | v19.5.0 | **v20.0** |
|---|---|---|
| 定性 | **纪律版** | **架构版** |
| 改了什么 | 发布流程 —— **零运行时行为变更** | 记忆的**所有权模型**（数据面契约） |
| 一句话主题 | 别把不该说的说出去 | 别把不该混的混在一起 |
| 该不该升 | 可以不升，不影响功能 | **建议升** —— 它修的是一类静默的数据覆盖 |
| 用例总数 | 约 700 | **1267** |

三条理由，一条比一条硬：

**① 记忆不再是一个大池子 —— 而且这不是「加个字段」。**

v19 之前，「这条记忆属于谁、属于哪个领域」是靠 `source` / `agent_id` 这类**渠道标记兼职表达**的。但渠道不是所有权。两个互不相干的记忆域一旦撞上同一个键，**后写的直接盖掉先写的 —— 两边都不报错，没有任何日志，没有任何计数会变**，先写的那条就那么没了。

v20 把「作用域」从一种**约定**变成一份**契约**：写、查、删、恢复、统计、反馈、后台任务、事件账本、毕业链、人格画像 —— 每一条在线读写路径都必须**显式说出自己在哪个域上工作**，说不出来的在取数之前就抛错，绝不静默降级成全库扫描。

迁移全程 additive：**存量数据一行没改、一行没删**，老数据一律落 `default` 域，键的形状与 v19 逐字相同。

**② 评测从「自己说」变成「可复现协议」。**

新增 `benchmarks/`：数据集、模型、judge、prompt、seed、文件哈希全部锁死留证，适配器走**真实 HTTP 契约**而不是进程内捷径 —— 否则测的是自己的函数，不是这个服务。oracle 只作检索上限诊断，**不进 headline**。

**③ 发货的部署物不再默认以 root 运行。**

systemd 单元与容器镜像全程 root 是既成事实。本版把两个单元降到专用账号、容量能力清零、文件系统只读、系统调用受限。生产实机前后对照（都由 `systemd-analyze` 自己算）：**暴露分 9.6 UNSAFE → 1.7 OK，capability 41 项 → 0 项**。

---

## 为什么不再用希腊神话命名

从 v9 到 v19，每个大版本都有一位神：Mnemosyne、Chronos、Aegis、Pantheon、Zeus、Athena…… 神格即架构，这套命名帮我们把架构讲清楚过，**它没有错，只是长到了该退场的位置**。

退场有三个具体理由，都不是审美问题：

**一、代号爬进了机器契约。** 代号本该只是给人看的诗意，但它渗进了模块名、日志前缀、健康探针的键名。于是「换个代号」不再是改文案，而是**动一次机器契约** —— 生产侧的日志采集按前缀过滤，改名之后服务照常起、日志照常写，只是**再也没被采集到**。这类改动的代价和它的收益完全不成比例。

**二、版本号读不出轻重。** `v19.4.2` 这样的四段式再挂一个代号，读者无法判断哪个是大版本、哪个是补丁。而版本号的第一职责就是回答这个问题。

**三、神格是承诺，堆多了就还不上。** 十几位神并列时，每一位都在暗示「这里有一套完整的能力」。但真实的软件是有的地方厚、有的地方薄 —— 用神名把薄的地方也说得很厚，是一种**不易察觉的过度宣称**。

所以自 v20.0 起：**版本号回归两段式，运行时不再设代号。**

但这不是否认历史 —— 下方〈诸神谱系〉整张表都留着，因为那些名字对应的能力**一个都没删，全都还在跑**。它们从"运行时的身份"退回"演进史里的路标"，各归其位。

---

## 和同类记忆系统比，我们强在哪、弱在哪

先说结论：**我们和「零依赖纯本地」那一类，不是同一种产品**，不存在谁追赶谁。它们优化的是「一个 pip 装完、不联网、亚毫秒」；我们优化的是「多租户、可治理、每一次改动都留账」。

### 能力面（定性）

| 能力 | aiduMEI | 零依赖本地类 | 云端托管类 |
|---|:---:|:---:|:---:|
| 多租户 / 记忆域隔离 | **✅ `(user_id, bank_id)` 二维契约** | ✗ 明示为单机单智能体设计 | 部分支持 |
| 双时间轴（记忆会**失效**而不是被删） | **✅ `valid_from` / `valid_to`** | ✗ | ✗ |
| 写入治理 + 人工复审 | **✅ 规则同步 + LLM 异步双审** | ✗ | ✗ |
| 事件账本（谁在何时改了什么） | **✅ 全路径留痕** | ✗ | 部分 |
| 原文保真（原话逐字留存，不只留蒸馏结果） | **✅ Verbatim Vault** | ✗ | ✗ |
| 跨机联邦（多 Agent 共享一套记忆） | **✅ 联邦身份 + MoE 门控** | ✗ | 部分 |
| 重排（cross-encoder 真重排） | **✅ bge-reranker-v2-m3** | ✗ 只做加权融合 | 部分 |
| 嵌入模型 | **bge-m3 · 1024 维 · 多语言** | 本地小模型 | 各家不同 |
| 零依赖 / 完全离线 | ✗ **需要嵌入与重排服务** | **✅ 这是它们的强项** | ✗ |
| 亚毫秒延迟 | ✗ | **✅ 这是它们的强项** | ✗ |
| 免费 / 自托管 | ✅ MIT，自托管 | ✅ | 多为付费 |

> ⚠️ **一句必须说清的边界：多租户 ≠ SaaS 安全边界。**
> aiduMEI 是一个**单机自托管**引擎。租户维度分的是**同一个部署内部**不同 Agent / 身份的记忆归属，
> 它**不是**用来把互不信任的外部客户放进同一台机器的隔离层。想要那种隔离，请一个客户一个部署实例。
> 我们把这句写在这里，是因为「租户」这个词很容易被过度理解 —— 而过度理解会带来真实的安全误判。

### 我们连外部模型换到了什么

这是个**取舍**，不是缺点，值得说清楚：

- **重排 ≠ 加权融合。** 加权融合是把已有的分数加一加；cross-encoder 重排是把问题和文档**放在一起重读一遍**再打分。长文档检索上这一步通常值 10–20 个点。
- **嵌入模型的档位差是实的。** 要塞进「零依赖 + 亚毫秒」，嵌入模型必然是小模型。我们用 bge-m3（1024 维、多语言、中文尤强）。
- **LLM 换来了抽取质量。** 零依赖意味着没有 LLM，那么「自动整合/总结」只能靠规则或截断。我们的事实抽取、冲突消解、治理评估都由 LLM 承担。

**代价我们也照实说：要联网、有延迟、有 API 成本。** 想要完全离线、亚毫秒的场景，那一类确实更合适 —— 这句话我们不遮。

### 读别人的数字时请注意口径

不针对任何一家，这是所有自公布跑分的通病，**下面两条都是拿对方自己页面上的两个数字互相对照**：

| 头条数字 | 同一页面上的另一个数 |
|---|---|
| 「**<1ms** 查询延迟」 | 自己的速度表：搜索 **45ms**、向量搜索 **15ms**（且是 **1000 条**规模下） |
| 「**98.9%** LongMemEval」 | 那是 **Recall@All@5**（答案在前 5 条里吗）；同页**端到端问答只有 65.2%** |

两点结论：

1. **1ms 是数据库裸读一条记录，不是一次语义检索。** 你不可能在 1ms 内跑完一次 cross-encoder 重排或一次 LLM 抽取 —— **那个数字本身就是「没做这些」的收据。**
2. **`Recall@k`（找得到）和端到端准确率（答得对）不是同一个指标**，公开资料里两者常差 30 个点以上。而且一个指标做到接近 99% 通常意味着它**已经饱和、失去区分度** —— 拿饱和指标当头条是一种选择。

---

## 关于跑分：我们的态度

**本版没有跑分，所以本页不宣称任何分数。**

我们也在积极准备自己的跑分，像其他友商一样努力 —— 区别在于我们打算**连复现方法一起交出去**。

评测协议已经就位（`benchmarks/`）：数据集、模型、judge、prompt、seed、文件哈希全部锁定留证，适配器走真实 HTTP 契约。也就是说我们公布分数的那一天，你拿同一份协议应该能跑出同一个数 —— **这才是我们认为分数唯一有意义的形态。**

为什么我们宁可先交白卷也不先放个数字：

- **这一版的规矩就是「宣称即承诺」。** 我们甚至为了一句措辞专门拆掉过一条断言 —— README 里那句「全轴齐备：推导值，从未实测」在我们真跑出来之后变成了假话，于是当天就改成带日期的实测值。在同一页放一个猜的分数，是自己拆自己的台。
- **第一次跑分的价值是暴露问题，不是拿分数。** 开发中我们反复见过「看着能用、实际空转」：推理模型在 token 上限下每题返回空、成绩单却报 0 错误；嵌入维度参数出厂就带着错的发货。真跑分大概率先炸出几个这类问题，第一次的数会明显低于第二次 —— 那也是它该有的样子。

**近期完成正式跑分，届时公布完整可复现记录（含失败项，不只贴好看的）。**

---

## 30 秒上手

### 方式一：GitHub 源码运行（官方源码渠道，含控制台）

```bash
# 1. 克隆
git clone https://github.com/monkey2jack/aiduMEI.git
cd aiduMEI

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
# 控制台打开 http://localhost:8767/ui/
```

> 💡 想让相关性闸门认得你自己的人名/项目代号？把它们填进环境变量 `AIDUMEM_ENTITY_KEYWORDS`，用 `|` 分隔，重启即生效。

---

## 📦 部署要求——轻到能塞进一台入门云主机

> 大家关心的问题：这套东西部署起来重不重、要多少内存 CPU、体积多大？**答案是：非常轻。** 这本身就是 aiduMEI 的一个设计亮点。

| 维度 | 实测数据 | 说明 |
|------|----------|------|
| **运行内存** | **约 210 MB RSS**（单进程实测） | 生产环境单个 Python 进程常驻，含 mem0 内核 + FastAPI + 嵌入式向量库 |
| **CPU** | **2 核足够，闲时 < 1%** | 无常驻重计算；LLM/Embedding 全部走外部 API，本机只做检索融合与 SQL |
| **磁盘（程序）** | 源码约 2.6 MB · 依赖 venv 约 175 MB | 纯 Python，无需编译；克隆即跑 |
| **磁盘（数据）** | 千级记忆约 13 MB 向量 + 数百 KB SQLite | 随记忆量线性增长，量级极小 |
| **直接依赖** | **仅 9 个顶层包** | mem0ai / qdrant-client / fastapi / uvicorn / pydantic 系 / httpx / requests |
| **Python** | 3.10 – 3.12 | 3.12 为推荐 |
| **前端** | **0 依赖** | 控制台纯静态，不装 node、不打包、不编译 |

**为什么这么轻，是刻意设计：**

- **向量库嵌入式落盘，不起独立服务**：Qdrant 走 `path: ./data/qdrant` 本地模式，无独立进程、无 Docker、无额外端口——省掉一整套向量数据库运维。
- **算力外包**：LLM、Embedding、Rerank 全部通过 OpenAI 兼容 API 外部调用，本机不加载任何大模型权重，因此不吃 GPU、不吃大内存。
- **相关性闸门先拦一道**：日常闲聊不触发检索，Token 与算力消耗直接省掉一个量级。
- **SQLite + FTS5 兜底**：结构化知识与全文搜索用零依赖的 SQLite，向量服务超时可热切换到本地全文搜索，不会因为向量库抖动而全盘瘫痪。

> 一句话：**一台 1 核 1G 的入门云主机即可跑起来，2 核 2G 从容有余。** 重量级的部分（大模型推理）都在云端 API，本地只是一个轻巧的记忆与检索大脑。

---

## 架构

```
┌──────────────────────────────────────────────────────────┐
│           aiduMEI⚕爱嘟优忆思 v20.1            │
│              FastAPI REST API :8767                       │
│              控制台 /ui :8767（自带静态托管）              │
│              MCP Server :8768 (41 tools)                  │
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

## aiduMEI 的独特之处

### 🖥️ 自带可视化控制台（v18.2 全新）

别家的记忆引擎给你一坨 API，自己写前端。aiduMEI 把控制台焊在后端里——克隆即用，六个面板覆盖记忆引擎的完整生命周期。**RECALL 追忆面板**是灵魂：它不只告诉你"存了什么"，而是用召回漏斗 trace 给你看"它凭什么想起这条"——候选池 8 条 → 点火 → 去重 → 时间衰减 → 最终 3 条，每步耗时与命中数全可见。纯静态前端，不装 node、不打包、不编译（MAP 星图面板用到 ECharts CDN，离线环境该面板降级）。

### 🔮 相关性闸门（Tahoe-Gate）
普通 RAG 系统对每条消息都去搜索记忆。aiduMEI 的**相关性闸门**（`GET /gate`）用启发式 + 动态实体匹配判断当前消息是否真的需要记忆检索。日常闲聊直接跳过检索 → 省掉无谓的向量召回开销，节省 Token 与算力。宿主 Agent 在注入记忆上下文前先问一句闸门即可。

### 🌊 潮浪并忆（Mnemosyne Tidal）
短消息不逐条调用 LLM。异步缓冲后按 session 分组，一次 LLM 调用处理多条。Tech / intimate / default 三档策略，快冲慢攒各取所需。

### ⏳ 三轨遗忘曲线（Ebbinghaus Decay）
记忆有保质期。Identity 和 Preference 是永久轨道（零衰减），Emotion 是加速衰减（1.5 倍），一般事实按标准遗忘曲线自然消退。**让 AI 学会忘记不重要的事。**

### 🕰️ 克罗诺斯双时间轴（Chronos Dual Timeline）
`valid_from` / `valid_to` 时间窗口：过期事实降权但不删除，未生效事实排在后面。所有铁律类记忆永不过期。

### ⚡ 原味抽屉（Raw Drawer — Zeus v18.0）
借鉴 MemPalace (58k⭐) 的 Verbatim Storage 理念。零 LLM 直存原始文本——代码片段、完整对话、原始日志，绕过 LLM 总结，一字不丢。FTS5 全文索引 + Qdrant 向量 + facts 登记，三路并行。

### 🔍 代码图谱（Code Graph — Zeus v18.0）
借鉴 code-review-graph (29k⭐) 的 AST 爆炸半径分析。用 Python 标准库 `ast` 解析项目依赖关系，改一个文件一秒告诉你影响范围。

### 📈 检索自进化（EvolveMem — Zeus v18.2）
借鉴 SimpleMem (3.7k⭐) 的进化理念。用户可对每次检索结果打分（useful / useless / correction），后台每 6 小时自动计算衰减/提权。高频优质词条自动沉淀，低质词条温柔降权。**闭环反馈，越用越聪明。**

### 🏛️ 万神殿联邦记忆（Pantheon Federation）
借鉴 MoE（Mixture-of-Experts）思想：底层建成完整的多 Agent 联邦基础设施，日常只激活当前 Agent 的热通道。

- **联邦身份**：每条记忆都带 `agent_id` / `profile` / `shared`，多 Agent 共用一套库互不污染
- **MoE 门控**：默认走热通道（一次 SQL，5ms 级）；仅在显式请求时才唤起其他 Agent
- **四级无缝降级**：L1 本 Agent → L2 分层加权 → L3 同 profile 联邦 → L4 跨 profile 全局
- **写入去重**：Jaccard 三态判定——≥0.85 合并、≥0.70 更新、<0.70 新增

### 🐙 冲突消解与技能结晶（Opus Octopod — v16.0）

- **ConflictResolver**：域名迁移、名称变更自动检测 + 旧值降权。双时间轴失效而非删除，保留完整历史
- **TreeMemory**：`node_path` 层级追溯，事实挂载到树状节点，支持向上追溯祖先
- **SkillCrystallizer**：后台自动感知高频重复事实，提炼为 Skill 候选。LLM 只能建议，**人工 approve 才生效**

### 🛡️ 埃癸斯护盾（Aegis — v14.0）
仓库里没有任何硬编码的身份、绝对路径、服务器地址或密钥。一切可变项走环境变量注入。克隆到任何目录、任何机器，`python api_server.py` 直接跑。

### 🌈 伊里斯彩虹桥（Iris — v15.0）
aiduMEI 提供 **Hermes Agent 官方 MemoryProvider 插件**，拿到全套生命周期钩子——turn 开头注入常驻块与相关检索、每轮后台归档、**压缩前把即将丢掉的对话先落进长期记忆**、镜像宿主内置 MEMORY.md 写入、三个可直接调用的工具。

```bash
cp -r integrations/hermes-plugin/aidumem ~/.hermes/plugins/
hermes config set memory.provider aidumem
```

### 🔧 零配置混合检索
BM25 trigram（零延迟兜底） + 向量嵌入 + Reranker 重排序 + 召回漏斗相关性排序。向量服务超时自动热切换到本地全文搜索。

---

## 核心接口

### 记忆操作

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/search` | 搜索记忆（混合：向量 + BM25 + 重排，直走 Workspace → Hybrid 混合召回） |
| `POST` | `/search_trace` | 带完整执行链路的搜索（召回漏斗 trace） |
| `POST` | `/add` | 添加记忆（默认异步潮浪合并；支持 `media_url` 多模态图片，v18.3） |
| `POST` | `/add/raw` | 原味抽屉——零 LLM 直存原始文本 |
| `DELETE` | `/delete` | 按 ID 删除记忆 |
| `GET` | `/health` | 健康检查 + 全探针诊断 |

### 控制台配置（v18.2 新增）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/config` | 模型配置只读视图（api_key 脱敏；v18.3 起含 vision 段与 features 开关） |
| `GET` | `/config/_speed` | 速度/合并可调参数 |
| `POST` | `/config/_speed` | 在线微调参数（写入 mem0_config_local.json） |
| `POST` | `/config/password` | 修改 UI 登录密码（v18.3，写入 .env 重启生效） |

> 前端控制台以 `/api` 为调用根（`API.base = '/api'`）。后端挂了一个 `/api` 别名子应用，让 `/api/stats`、`/api/config` 等直接命中扁平路由，无需改前端。访问 `/` 自动重定向到 `/ui/`。

### 多模态视觉记忆（Zeus v18.3）

`/add` 原生支持多模态：传入 `media_url` 或 `image_url`，后端自动调用 OpenAI 兼容 Vision API 生成图片描述（`vision_caption`）并入库。支持三种图片输入：

```json
{
  "messages": [{"role": "user", "content": "这张照片是我拍的海边日落"}],
  "metadata": {
    "media_url": "https://example.com/sunset.jpg",
    "category": "moment"
  }
}
```

- **远程 URL**：`https://...`
- **Data URI**：`data:image/jpeg;base64,...`
- **纯 Base64**：`/9j/4AAQ...`（自动补齐 data 前缀）

Vision 模型在 `mem0_config_local.json` 的独立 `vision` 配置段指定（缺省 fallback 到 `llm` 段）。

### Obsidian 双链联动（Zeus v18.3）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/obsidian/sync` | 接收 Obsidian 笔记推送，解析 `[[Wikilink]]` 双链并沉淀为实体图谱节点 |

### 无损秒级升级（Zeus v18.3）

基于 `PRAGMA user_version` 的 schema 版本化机制，代码更新与数据重构彻底解耦：纯逻辑更新直接重启生效，表结构变更在启动瞬间以 `ALTER TABLE ADD COLUMN` 毫秒级完成，**不破坏任何存量数据**。详见 [Fast-Update SOP](docs/Fast_Update_SOP.md)。

### 代码图谱（Zeus v18.0）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/code/impact` | 分析文件改动波及范围（爆炸半径） |
| `GET` | `/code/graph` | 查看全项目代码依赖图 |

### 检索自进化（Zeus v18.2）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/evolve/feedback` | 提交检索质量反馈（useful / useless / correction） |
| `GET` | `/evolve/report` | 进化统计面板（召回率、权重调整历史） |

### 八爪鱼治理（Opus v16.0）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/conflict/resolve` | 冲突消解（域名迁移、名称变更自动降权） |
| `GET` | `/tree/nodes` | 树状记忆图谱节点列表 |
| `POST` | `/crystals/detect` | 检测可结晶的高频重复事实 |
| `GET` | `/crystals` | 查看技能结晶候选项 |

### 🦉 Athena 认知层（v19.0 新增）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/reflect` | 触发主动反思，提炼模式/矛盾/知识缺口为洞察 |
| `GET` | `/reflect/list` | 列出已落库的反思洞察 |
| `GET` | `/reflect/context` | 取可注入上下文的反思摘要 |
| `GET` | `/self-edit/edits` | 查看记忆自编辑（合并/冲突）历史 |
| `POST` | `/self-edit/rollback` | 回滚一次自编辑（旧内容还原） |
| `GET` | `/memory/types` | 六种记忆类型定义与分布 |
| `POST` | `/memory/types/query` | 按类型检索记忆 |
| `POST` | `/memory/types/backfill` | 给存量记忆回填类型标签 |
| `POST` | `/memory/refine` | 触发递归精炼（多条碎记忆 → 高层抽象） |
| `POST` | `/memory/refine/apply` | 应用一条精炼产物 |
| `POST` | `/memory/refine/rollback` | 回滚精炼（原记忆还原） |
| `GET` | `/memory/refinements` | 精炼产物列表 |
| `POST` | `/skill/grow` | 从任务轨迹生长 SKILL.md 草稿（待人工 approve） |
| `GET` | `/skill/drafts` | 技能草稿列表 |
| `POST` | `/crystals/use` | 技能复用打点（成功/失败计数） |
| `POST` | `/crystals/prune` | 淘汰低效用技能（标记 archived，不删除） |

### 🎭 人格记忆基座（Persona Memory Layer · v19.0）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/persona/build` | 构建人格基座（`synthesis` 合成 / `grounded` 落地 双模式） |
| `GET` | `/persona/banks` | 人格库列表 |
| `GET` | `/persona/detail` | 单个人格库的 L/G/E 三层明细 |
| `POST` | `/persona/retrieve` | 按情境检索人格记忆 |
| `GET` | `/persona/context` | 取可注入的人格上下文 |
| `POST` | `/persona/rollback` | 回滚到人格库的历史版本 |

### 万神殿联邦（Pantheon v13.0）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/federation/recall` | 联邦检索（MoE 门控自动决策热/联邦通道） |
| `POST` | `/federation/facts/add` | 联邦写入（自动去重 + 分层 + 归属） |
| `GET` | `/federation/agents` | Agent 列表（含事实数与在线状态） |
| `POST` | `/federation/agents/register` | 注册 Agent 到联邦 |
| `GET` | `/federation/broadcast` | 拉取其他 Agent 的新共享事实 |
| `GET` | `/federation/awareness` | 联邦态势摘要 |

### 示例

```bash
# 搜索记忆
curl -s -X POST http://localhost:8767/search \
  -H "Content-Type: application/json" \
  -d '{"query": "我之前说过项目截止日期是什么？", "user_id": "me", "limit": 5}'

# 带召回漏斗 trace 的搜索
curl -s -X POST http://localhost:8767/search_trace \
  -H "Content-Type: application/json" \
  -d '{"query": "张伟的职业是什么", "user_id": "default", "limit": 3}'

# 添加记忆
curl -s -X POST http://localhost:8767/add \
  -H "Content-Type: application/json" \
  -d '{"messages": "[{\"role\":\"user\",\"content\":\"项目截止日期是3月15号\"}]", "user_id": "me"}'

# 原味抽屉——直存代码片段，不走 LLM
curl -s -X POST http://localhost:8767/add/raw \
  -H "Content-Type: application/json" \
  -d '{"content": "def hello(): print(\"Hello World\")", "source": "my_script.py", "user_id": "me"}'

# 读取模型配置（控制台用）
curl -s http://localhost:8767/config | python -m json.tool
```

---

## 接入 Hermes Agent

| 方式 | 能力 | 何时用 |
|------|------|--------|
| **A. MemoryProvider 插件**（推荐） | 全生命周期钩子 + 工具 + 备份 | 默认选这个 |
| **B. Shell Hook** | 仅 turn 开头注入 | 宿主不方便装插件时 |

两种方式**不要同时开**（会重复注入白烧 token）。完整步骤、验证方法与回滚见 [integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md)。

> ⚠️ **安全**：aiduMEI 服务自身不做鉴权，默认只监听 `127.0.0.1`。要跨机访问请在前面挂带认证 + TLS 的反向代理，别把服务直接暴露到公网。

---

## MCP Server（41 工具）

aiduMEI 内置 MCP Server（`:8768`），暴露 41 个工具，分组如下：

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

- **运行时**：Python 3.12+、FastAPI、Uvicorn
- **记忆内核**：mem0 v2.0.19（v20.1）
- **向量存储**：Qdrant（通过 qdrant-client）
- **结构化数据**：SQLite（facts.db、observations.db、scenes.db、fact_events.db）
- **全文搜索**：SQLite FTS5 + trigram 分词器
  - **中文切词策略（v19.4.1 P1-2 更正）**：trigram 分词器索引的是 3 字符窗口，因此中文查询按 **3-gram** 切词才能命中索引。v19.4.0 及之前切 2-gram，与索引失配——中文查询实际一直落在 `LIKE` 全表扫描上（20 万条原文实测稀有词 32.8 ms）。现已对齐，同量级降至 0.05 ms。
  - **trigram 的固有边界**：不足 3 字的查询（如「祖母」）无法用 trigram 表达，由 `LIKE` 兜底。这是分词器定义决定的，不是缺陷；召回结果的 `_recall_path` 字段（`fts` / `like`）会如实标注本次真走的哪条路，降级不再静默。
- **向量化**：可配置（兼容 OpenAI Embedding API）
- **重排序**：可配置（兼容 OpenAI Rerank API · 多 provider 抽象：OpenAI-compatible / Jina / Cohere）
- **大模型**：兼容任何 OpenAI 格式的 API
- **MCP**：fastmcp stdio + HTTP 双模
- **控制台**：零依赖纯静态（HTML + CSS + 原生 JS + ECharts CDN），由后端 `/ui` 直接托管

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

| 维度 | 现状 |
|------|------|
| 用例总数 | **1267**（`pytest --collect-only` 实测） |
| 独立开发机 | 1255 通过 · **12 跳过** —— 缺宿主 Hermes 源码，有 git 工作区（实测） |
| 生产机沙箱 | 1266 通过 · **1 跳过** —— 有宿主 Hermes 源码，无 git 工作区（沙箱是白名单拷贝，不含 `.git`）。**这一行是按轴推导**：1267 减去 git 工作区那条轴门控的 1 条；最后一次真实沙箱实测是 **859 通过 · 1 跳过**，跑在 v20.0 提交树上（当时总数 860） |
| 全轴齐备 | 1267 全绿 · 0 跳过 —— **2026-08-26 生产机候选树实测**（bundle 克隆含 `.git`，十一条轴同时齐备）。上一次全轴实测是 1254 全绿（同日早前的 v20.1.1 候选树）。更早这一行是推导值，标注为「从未实测」长达数个版本 |
| 层级 | 以**模块级单元测试 + 源码级守卫断言**为主，`TestClient` 驱动的接口测试为辅 |
| 平台前提 | 全量套件按 **Linux/macOS（POSIX）**口径维护：`backup_gate` 轴要 POSIX shell；`/health` 的 CPU/RSS 指标走 `resource` 模块，非 POSIX 平台诚实置 `None` 不崩（v20.1 整改）。Windows 未列为全量测试平台 |
| 语句覆盖率 | 约 51%（`ducky/` + 入口，`coverage` 实测） |
| 未覆盖 | 真实 mem0 / Qdrant 集成、真实 LLM 调用、并发压测 —— 这些依赖外部服务，由生产环境实机冒烟承担 |

> **为什么要把 1255 和 1266 都写出来**：同一份测试集在不同环境下跑出不同数字，只报其中一个都会误导读者。
> 1255 是本机实测；1266 是**按轴推导**（1267 减去 git 工作区那条轴）—— 最后一次真实沙箱实测是 859，
> 跑在 v20.0 提交树上，那时总数是 860。哪个数是实测、哪个数是推导，必须逐个交代清楚。
> 引用测试数字时请连带说明运行环境 ——
> 这本身就是「宣称即承诺」纪律的一部分。
>
> **跳过不止一条轴**（v20.0 实测补正）：此前这一段只认「宿主 Hermes 源码」一条轴，于是把「全绿」
> 当成了装上宿主就能拿到的东西。生产实跑打脸 —— 沙箱里宿主明明在场，跑出来**仍有 1 条跳过**。
> 全量普查后，跳过其实有**十一条互不相干的轴**（v20.1 补第十条 mem0 基座；v20.2 补第十一条 fastembed 备胎）：
>
> | 跳过轴 | 门控用例数 | 位点 |
> |--------|-----------|------|
> | 宿主 Hermes 源码 | 12 | `tests/test_hermes_plugin.py` 整份 |
> | git 工作区 | 1 | `tests/test_v20_brand_policy.py`（要 `git ls-files` 当比对基准） |
> | `scripts/backup_gate.sh` + POSIX shell | 7 | `tests/test_v19_4_1_backup_gate.py` 整份 |
> | `qdrant_client` 已安装 | 1 | `tests/test_v20_vector_bank_contract.py` |
> | LoCoMo 数据集已就位 | 1 | `tests/test_v20_locomo_official.py`（全量数据集扫描要真文件） |
> | `regex` 已安装 | 1 | `tests/test_v20_locomo_official.py`（拿 `regex` 给标准库 `re` 对拍） |
> | `numpy` 已安装 | 1 | `tests/test_v20_locomo_official.py`（拿 `numpy.mean` 给 `sum/len` 对拍） |
> | `nltk` 已安装 | 13 | `tests/test_v20_locomo_official.py` 与 `tests/test_v20_benchmarks.py`（官方 F1 的 PorterStemmer，换实现就不是官方口径） |
> | `git` 可执行文件在场 | 6 | `tests/test_v20_gitignore_guard.py` 整份（拿一个临时空仓当 ignore 判据，不碰本仓的 `.git`） |
> | `mem0ai` 已安装 | 20 | `tests/test_v20_mem0_patch_layer.py` 整份（补丁层疗法要真实基座在场；此前缺 mem0 是 20 条 ERROR 冒充真缺陷，现在诚实跳过） |
> | `fastembed` 已安装 | 1 | `tests/test_v20_2_autoshift.py`（自动挡备胎真模型测试；缺依赖诚实跳过，模型未部署时用例内二次跳过） |
>
> 开发机缺第一条 → 1255 + 12；生产机沙箱缺第二条（白名单拷贝没有 `.git`）→ 1266 + 1。
>
> **「全轴全绿」是实测值**（2026-08-25 生产机候选树，总数 1200、九轴齐备、0 跳过；上一次是 2026-08-24 的 1112）。这一行的来历值得留着：
> 更早的一版 README 把它写成「生产实跑核验」，而它恰恰被自己引用的那次生产实跑当场证伪；
> 于是改成「推导值，从未实测」，并配了一条**会随环境失效**的断言把这句话钉住 ——
> 真有一天齐备了，那条断言就该红。它在 2026-08-24 按设计红了，那一版才换成实测值。
> **绝对措辞必须经得起自己引用的那次测量**：先让它可证伪，再谈它是真的。
> 注意这台实机**不是**上面那行的「沙箱」（沙箱是不含 `.git` 的白名单拷贝），两个数不是同一棵树。
>
> ⚠️ **这一行说的是沙箱，不是线上部署树**（v20.0 部署前踏勘发现）：线上那棵树里躺着一份陈旧的
> `.git`（早年 clone 留下的，如今靠文件拷贝更新，索引早已落后于代码），所以 git 工作区这条轴
> 在**那里不会跳** —— 它会拿一份过期的 `git ls-files` 当基准去比对，那是另一种错，不是同一个数。
> 上一版把这行叫「生产部署树」，行名和它自己的括注（「生产机沙箱实测」）当场互相矛盾，而我一直
> 没看见。**测的是哪棵树，就只许报哪棵树。**
>
> **这 12 条不是玄学，自己就能验**（v19.4.2 补）：它们全在 `tests/test_hermes_plugin.py`，
> 跳过条件是宿主 `agent/memory_provider.py` 找不到。`HERMES_SRC` 三态可控，**两个方向都能复现**：
>
> ```bash
> pip install -r requirements-dev.txt                            # 跑测试要 pytest；requirements.txt 里不含它
> pytest tests/ -q -rs | tail -1                                 # 无宿主：1255 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # 有宿主：1267 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # 装了宿主也强制关掉，照旧 1255 passed, 12 skipped
> ```
>
> 「跳过」必须能被复现成「通过」，**反过来也必须成立**。机器上恰好装着宿主时（`/hermes/hermes-agent`
> 会被自动发现，我们自己的生产机就是这样），上面第一条命令跑出来其实是 1266 passed、1 skipped
> （**这个数是按轴推导的**：最后一次真实沙箱实测是 859 passed、1 skipped，跑在总数 860 的 v20.0 提交树上）——
> 剩下那 1 条卡在 git 工作区那条轴上（沙箱是白名单拷贝，树里没有 `.git`）。上面代码块里的
> `有宿主：1267 passed` 要十一条轴同时齐备才拿得到 —— 2026-08-26 在生产机候选树上**实测到**
> （总数 1267，十一轴齐备、0 跳过）。但别把「装上宿主」当成「全绿」：宿主只是十一条轴里的一条。
> 没有 `HERMES_SRC=none` 这一档，读者根本无法在自己机器上把我们宣称的「12 跳过」复现出来。
> **双向可复现才叫可证伪**：一个你没法让它跳过的「跳过」，和一个你没法让它通过的「通过」，同样不可信。
>
> 另外：`HERMES_SRC` 指向的路径若不含 `agent/memory_provider.py`，会**直接报错**，
> 而不会静默回退到自动发现的路径 —— 指了 A 却在测 B 还给绿灯，是最难发现的一种假绿灯。

**为什么把这些写清楚**：v19.4.0 的 README 只写「全量测试 244 通过」，读者会理解为端到端保障。但 244 用例 0.88 秒跑完，显然不含任何真实外部依赖。更关键的是——v19.4.0 的幂等去重测试是绿的，却只覆盖了带显式时间戳的 `list[dict]` 载荷，而生产实际走的是无时间戳的纯字符串载荷，真 bug 就从这条缝里带着绿灯上线了。

因此 v19.4.1 起执行**反假绿灯纪律**：涉及载荷形态、凭据形态、查询形态的测试一律多形态并测；性能与索引类断言必须校验 `_recall_path` 这类自证字段，而不是只看「有没有命中」。

---

## 已知例外与本版不覆盖

`(user_id, bank_id)` 作用域契约覆盖的是**在线读写路径**。以下三处是本版**明确不覆盖**的边界，写在这里，而不是留给读者自己在生产上撞：

| # | 例外 | 现状 | 为什么这一版不做 |
|---|------|------|------------------|
| 1 | **`core_memory` 键形状** | 表主键仍是单列 `block_key`（`ducky/core_memory.py`）。域隔离由唯一索引 `idx_core_memory_scope_key(user_id, bank_id, block_key_raw)` 与写侧「`DO UPDATE SET` 子句不含归属列」共同保证 | 改主键形状是**破坏性**变更，必须排在存量数据的域归属对账**之后**。顺序倒过来，会把尚未对账的存量数据按错误的域焊死 |
| 2 | **全库维护作业** | 记忆演化与显著性维护（`ducky/evolve_mem.py`、`ducky/routes_evolve.py`）**按全库扫描，不按域隔离**；源码 docstring 已就地标注 | 这类作业的语义就是全库维护，按域切分会让跨域的衰减与归并失去全局视野。它们**不进用户可见的检索路径** |
| 3 | **存量数据的域归属** | 自 v19 升级上来的存量记忆一律落 `default` 域，**未按真实归属对账** | additive 迁移的前提就是「存量一行不改、一行不删」。真实归属需要业务侧确认，属数据治理，不属这一次代码发布 |

**边界的边界**：例外 1、2 都不会导致跨域读到别人的数据 —— 1 由唯一索引兜底（写入永不改写归属列），2 不进用户检索路径。例外 3 的影响是「域标签不准」，不是「域与域之间串了」。

> 这一节的存在本身是一条纪律：只要 README 出现绝对化的隔离表述，就必须同时挂一份**已知例外清单**。声称「全量」而不列例外，等于把边界留给用户在生产上撞 —— 那正是这个项目反复付过学费的失败形态。

---

## 🖥️ aiduMEI 控制台（全新）

> **v18.2 起自带可视化控制台** —— 不再是纯 API 服务，而是一个"看得见记忆如何被想起"的引擎。

aiduMEI 内置一个轻量 Web 控制台，由后端直接托管在 `/ui`，无需单独部署前端（纯静态 HTML/CSS/JS，仅 MAP 星图面板用到 ECharts CDN）。六个面板覆盖记忆引擎的完整生命周期：

| 面板 | 代号 | 看什么 |
|------|------|--------|
| 💗 **PULSE 脉搏** | 服务状态 + 存储分层 | 版本/代号/核心模块在线数、四层记忆存量与容量 |
| 🗄️ **VAULT 记忆库** | 搜索 + 分类账本 | 语义检索（向量+重排）、6 知识域分类家底、最近写入的事实流 |
| 🗺️ **MAP 星图** | 知识域星图 | ECharts 力导向图：核心/知识域/分类/实体四类节点，可拖拽缩放 |
| 🔍 **RECALL 追忆** | 召回漏斗 trace | 候选池→点火→去重→时间衰减→最终，每步耗时与命中数全可见 |
| 🧬 **EVOLVE 进化** | 检索质量看板 | 7 天查询/命中/得分/零命中、进化周期日志、反馈信号 |
| ⚙️ **SETTINGS 设置** | 模型配置 + 模块 + 联邦 | LLM/Embedding/Reranker 配置只读（api_key 脱敏）、思考模式、可调参数、核心模块探针、联邦成员 |

> 本仓**不随附界面截图**——截图会随版本迅速过时，且容易把演示数据当成产品承诺。下面用文字说明每一屏实际显示什么；服务起来后访问 `/ui` 即可自行验证。仓库内 `tools/shot.js` 是我们自己用的面板截屏脚本（CDP 驱动、支持滚动容器），需要出图时可直接用。

### PULSE — 脉搏

服务健康与版本代号；11 个核心模块**逐个**探针（在线 / 降级 / 离线，降级会指名是哪一个模块，不是「服务活着就绿」）；四层记忆各自的存量与容量水位。

### VAULT — 记忆库

语义检索（向量召回 + 重排打分，结果带分数与来源域）；6 个知识域的分类家底（每域实际条数）；最近写入的事实流，时间倒序、可按域筛。

### MAP — 知识域星图

ECharts 力导向图，核心 / 知识域 / 分类 / 实体四类节点按真实库存量渲染，滚轮缩放、拖拽节点、悬停查看该节点下挂的事实条数与样例。

### RECALL — 追忆漏斗

> 这一屏是 aiduMEI 最想做好的地方：别家的记忆面板只给你"存了什么"，这里给你"它凭什么想起这条"。

候选池 → 🔥 点火 → 去重 → 时间衰减 → 最终，五个阶段**每一步的耗时与进出条数都摆在面上**：进来多少、被谁筛掉、剩下多少、最终为什么是这几条。零命中时同样出图——空结果也是结果，能看见空在哪一步发生。

### EVOLVE — 检索自进化

7 天检索质量看板：查询数、平均命中数、平均得分、零命中数四条曲线；下方是进化周期日志（每轮调了什么、依据是什么）与用户反馈信号。

### SETTINGS — 模型配置

LLM / Embedding / Reranker 配置**只读**展示，`api_key` 自动脱敏——面板刻意不提供改密钥的入口，配置只走服务端文件与环境变量；另有思考模式状态、可调参数、核心模块探针、联邦成员列表。

---

## 诸神谱系

> aiduMEI 的大版本曾以希腊神祇命名，神格即架构。自 v20.0 起版本号回归两段式、不再设代号——诸神留在谱系里作历史。

| 版本 | 代号 | 神格 | 核心使命 |
|------|------|------|----------|
| **v20.0** | —（无代号） | 记忆域隔离 · 作用域即契约 | **`(user_id, bank_id)` 二维作用域契约贯通在线读写路径（写查删恢复统计反馈账本与毕业链） · 非法作用域取数前即拒 · additive 迁移存量零改动 · `benchmarks/` 可复现评测协议 · `vector_backend` 后端契约与影子 POC · 观测面补齐域/后端/降级证据** |
| **v19.5.0** | **Athena** · 雅典娜 | 脱敏闸门 · 把铁律变成不可绕过的程序 | **七面扫描器焊入发布链（新增包索引渲染面）· 词表外置绝不入仓 · 空词表拒绝运行而非放行 · 负向对照焊进代码，自检本身可证伪 · 豁免只认本行且必须留在报告里** |
| **v19.4.3** | **Athena** · 雅典娜 | 发布卫生 · 发行包也是公开面 | **与 v19.4.2 行为逐字等价（仅注释与版本号）· 发行包解包实扫成为上传前的强制卡点 · 扫描器必须先经负向对照才算数** |
| **v19.4.2** | **Athena** · 雅典娜 | 守卫扩面 · 集成件凭据贯通 | **元测试锁死守卫射程 · 8 个凭据入口收敛到唯一真相源 · `.env` 兜底链贯通独立集成件 · 崩溃循环可见 · 日志切割不丢 · 字标残留清零 · 配置写了不等于配置生效（`StartLimit*` 段位）· 双向可复现才叫可证伪（`HERMES_SRC` 三态）** |
| **v19.4.1** | **Athena** · 雅典娜 | 审计补丁 · 鉴权贯通与租户闭环 | **一道门禁两把钥匙 · 租户可见性收窄与跨租户覆盖修复 · 级联删除补齐原文层 · 幂等键与中文索引根治** |
| **v19.4.0** | **Athena** · 雅典娜 | 明镜工程 · 原文保真 · 生产审计修复 | **Verbatim Vault 原文保真层 · 原文证据融合召回 · 注入框架服务端自防御 · LLM 通道根治 · 噪声规则升级 · 升级备份硬门禁 · 账本别名展开 · 次路径治理账本补齐** |
| **v19.3.3** | **Athena** · 雅典娜 | 架构大一统 · 审计驱动修复 | **打分单一真相源 · 单例并发加固 · 注入防御统一Gate · 静默异常可观测 · legacy解耦** |
| **v19.2.0** | **Athena** · 雅典娜 | 生产级加固 · 一致闭环 | **Prompt注入防护 · 多仓原子删除与WAL · 统一打分体系 · 动态健康观测** |
| **v19.0** | **Athena** · 雅典娜 | 智慧女神 · 从记忆到智慧 | **Reflect 主动反思 · 记忆自编辑去重 · 递归精炼 · Skill 自生长 · 人格记忆基座** |
| **v18.3** | **Zeus** · 宙斯 | 众神之王 · 多模态感知 | 无损秒级升级 · 多模态视觉记忆 · Obsidian 双链联动 · 控制台密码修改 |
| **v18.2** | **Zeus** · 宙斯 | 众神之王 · 检索自进化 | EvolveMem 反馈闭环、38 MCP 工具、质量审计全覆盖、**自带可视化控制台** |
| **v18.0** | **Zeus** · 宙斯 | 众神之王 · 吸星大法 | 原味抽屉 · 代码图谱 · 五大竞品精华融合 · MCP×36 · IDE 钩子 |
| **v17.0** | **Themis** · 忒弥斯 | 秩序女神 | 事件账本 · 敏感分档 · 治理铁律 |
| **v16.0** | **Opus Octopod** · 八爪鱼 | 深海智者 | 冲突消解 · 树状记忆 · 技能结晶 |
| **v15.0** | **Iris** · 伊里斯 | 彩虹信使 | 官方 MemoryProvider 通道 · 惰性热载 |
| **v14.0** | **Aegis** · 埃癸斯 | 神盾 | 零硬编码 · 隐私护盾 · 开箱可部署 |
| **v13.0** | **Pantheon** · 万神殿 | 众神之殿 | 多 Agent 联邦 · MoE 门控 |
| **v12.0** | **Chronos** · 克罗诺斯 | 时间之神 | 双时间轴有效期 |
| **v11.0** | **Hyperion** · 海伯利安 | 光明之神 | 线程本地连接池 · 性能纪元 |
| **v9.1** | **Mnemosyne** · 谟涅摩绪涅 | 记忆女神 | 潮浪并忆 · 双策分档 |

[完整版本演进史 →](CHANGELOG.md)

---

## 仓库结构

```
aiduMEI/
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

---

<p align="center">
  <sub>aiduMEI⚕爱嘟优忆思｜Powered by monkey²</sub>
</p>
